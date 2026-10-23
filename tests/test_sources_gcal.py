"""Google Calendar push source (contract C4, T6): the shared kit, the channel token, the header-built
payload, the ``sync`` ping, key per calendar, dedup per channel message, and parity with the bridge it
replaces (``gcal_bridge.py``: the token rule and the payload it forwarded). The bridge's in-memory
60 s debounce is replaced by the durable ``calendar`` span, asserted below.

Regenerate captures with ``python tests/test_sources_gcal.py --write``.
"""
from __future__ import annotations

import hmac
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sourcekit  # noqa: E402
from sourcekit import SENTINEL, Env, Kit, request  # noqa: E402

from hookspool import config as hs_config  # noqa: E402
from hookspool import plugin as P  # noqa: E402
from hookspool.__main__ import main  # noqa: E402
from hookspool.plugin import Request, parse_capture  # noqa: E402
from hookspool.sources import gcal  # noqa: E402

TOKEN = "test-gcal-channel-token-" + SENTINEL
URI = "https://www.googleapis.com/calendar/v3/calendars/primary/events?alt=json"


def push_headers(state="exists", channel="chan-1", number="7", uri=URI, resource="res-" + SENTINEL) -> dict:
    h = {"X-Goog-Channel-ID": channel, "X-Goog-Resource-ID": resource, "X-Goog-Resource-State": state,
         "X-Goog-Message-Number": number, "User-Agent": "APIs-Google", "Content-Length": "0"}
    if uri is not None:
        h["X-Goog-Resource-URI"] = uri
    return {k: v for k, v in h.items() if v is not None}


def sign(raw, secrets, now):
    return raw.set("X-Goog-Channel-Token", secrets["channel_token"])


KIT = Kit(name="gcal", module=gcal, secrets={"channel_token": TOKEN}, sign=sign, body_signed=False,
          sample=request("POST", "/hooks/gcal", push_headers()))


@pytest.mark.parametrize("case", sourcekit.CASES, ids=sourcekit.CASE_IDS)
def test_kit_matrix(case, tmp_path, clock):
    sourcekit.run(case, KIT, tmp_path, clock)


CAPTURES = sourcekit.captures("gcal")


@pytest.mark.parametrize("path", CAPTURES, ids=[os.path.basename(p) for p in CAPTURES])
def test_captures(path, tmp_path, clock):
    assert sourcekit.run_capture(path, KIT, tmp_path, clock)["source"] == "gcal"


def test_captures_cover_the_outcomes():
    got = {}
    for c in CAPTURES:
        with open(c, "rb") as f:
            got[os.path.basename(c)] = parse_capture(f.read()).meta["expect"]
    assert got.get("event.http") == "queued" and got.get("sync.http") == "ignored", got
    assert got.get("wrong_token.http") == "unverified" and got.get("no_token.http") == "unverified", got


@pytest.fixture
def env(tmp_path, clock):
    e = Env(KIT, tmp_path, clock)
    yield e
    e.close()


def post(env, token=TOKEN, **hdr):
    raw = request("POST", "/hooks/gcal", push_headers(**hdr))
    if token is not None:
        raw = raw.set("X-Goog-Channel-Token", token)
    return env.send(raw)


def status(r):
    return r.status, (json.loads(r.body)["status"] if r.body else "")


# ---- token and sync ---------------------------------------------------------------------------------------
def test_wrong_missing_and_empty_tokens_are_401(env):
    for tok in ("nope", None, "", TOKEN[:-1], TOKEN + "x"):
        r = post(env, token=tok)
        assert (r.status, r.body) == (401, b""), tok
    assert env.events() == []


def test_a_sync_ping_is_verified_then_acknowledged_and_not_enqueued(env):
    assert status(post(env, state="sync", number="1")) == (200, "ignored")
    assert env.batches() == [] and env.events() == []
    assert post(env, state="sync", token="nope").status == 401, "sync is verified like everything else"


def test_exists_and_not_exists_are_events(env):
    assert status(post(env, state="exists", number="2")) == (200, "queued")
    assert status(post(env, state="not_exists", number="3")) == (200, "queued")
    assert [e["event_type"] for e in env.events()] == ["exists", "not_exists"]


def test_parity_with_the_bridge_token_rule():
    """gcal_bridge.do_POST: ``hmac.compare_digest(X-Goog-Channel-Token, CHANNEL_TOKEN)``. The plugin also
    refuses an empty token (the bridge generated one on first run, so it was never empty)."""
    for sent in ("", TOKEN, "x", TOKEN.upper(), TOKEN[:-1]):
        req = Request("POST", "/hooks/gcal", [("X-Goog-Channel-Token", sent)] if sent else [], b"", now=0)
        assert gcal.verify(req, {"channel_token": TOKEN}) is hmac.compare_digest(sent, TOKEN), sent
    req = Request("POST", "/hooks/gcal", [], b"", now=0)
    assert gcal.verify(req, {"channel_token": ""}) is False


# ---- payload, key, dedup --------------------------------------------------------------------------------------
def src():
    return P.Source("gcal", gcal, {})


def classify(**hdr):
    h = [(k, v) for k, v in push_headers(**hdr).items()]
    return src().classify(Request("POST", "/hooks/gcal", h, b"", now=0))


def test_payload_is_what_the_bridge_forwarded():
    c = classify()
    # gcal_bridge.do_POST built exactly these keys from the same headers
    assert c.payload == {"event_type": "exists", "channel_id": "chan-1", "resource_id": "res-" + SENTINEL,
                         "resource_state": "exists", "resource_uri": URI, "message_number": "7",
                         "source": "google-calendar"}
    assert c.event_type == "exists" and c.attrs == {"calendar": "primary", "state": "exists"}
    assert c.default_action == "script:gcal_delta"


def test_missing_state_is_unknown_like_the_bridge():
    h = push_headers()
    del h["X-Goog-Resource-State"]
    c = src().classify(Request("POST", "/hooks/gcal", list(h.items()), b"", now=0))
    assert c.payload["resource_state"] == "unknown" and c.payload["event_type"] == "unknown"


@pytest.mark.parametrize("uri,calendar", [
    (URI, "primary"),
    ("https://www.googleapis.com/calendar/v3/calendars/owner%40example.test/events?alt=json", "owner@example.test"),
    ("https://www.googleapis.com/calendar/v3/calendars/abc123@group.calendar.google.com/events", "abc123@group.calendar.google.com"),
    ("", "primary"), (None, "primary"), ("https://example.test/other", "primary"),
])
def test_key_is_the_calendar_of_the_resource_uri(uri, calendar):
    assert classify(uri=uri).key == calendar


def test_dedup_is_per_channel_message_number():
    assert classify(channel="c1", number="7").dedup_id == "c1:7"
    assert classify(channel="c2", number="7").dedup_id == "c2:7"
    assert classify(channel="", number="7").dedup_id is None
    assert classify(channel="c1", number="").dedup_id is None


def test_a_resent_message_number_is_a_duplicate_but_another_channel_is_not(env):
    assert status(post(env, channel="c1", number="7")) == (200, "queued")
    assert status(post(env, channel="c1", number="7")) == (200, "duplicate")
    assert status(post(env, channel="c2", number="7")) == (200, "queued")


def test_channels_of_one_calendar_coalesce_into_one_batch_on_the_calendar_span(env):
    """The old bridge debounced in memory per calendar; now it is durable and per calendar."""
    for i, ch in enumerate(("old-channel", "new-channel", "old-channel")):
        assert status(post(env, channel=ch, number=str(i + 1))) == (200, "queued")
    b = env.batches()
    assert len(b) == 1 and b[0]["key"] == "primary" and b[0]["count"] == 3
    assert b[0]["destination"] == "script:gcal_delta" and (b[0]["quiet_s"], b[0]["max_s"]) == (45.0, 180.0)


def test_another_calendar_is_another_batch(env):
    post(env, number="1")
    post(env, number="2", uri="https://www.googleapis.com/calendar/v3/calendars/other%40example.test/events")
    assert sorted(r["key"] for r in env.batches()) == ["other@example.test", "primary"]


def test_nothing_is_stored_but_the_derived_payload(env):
    post(env, number="9")
    assert env.events()[0]["raw"] in (None, b"")
    env.assert_no_secret_in_db()
    assert "X-Goog-Channel-Token" not in json.loads(env.events()[0]["headers"] or "{}")


def test_declarations():
    assert (gcal.span, gcal.sender_retries, gcal.action) == ("calendar", True, "script:gcal_delta")
    assert gcal.auth_headers == ("X-Goog-Channel-Token",) and gcal.keep_headers == ()


# ---- config -------------------------------------------------------------------------------------------------------
def _config(tmp_path) -> str:
    sec = tmp_path / "channel-token"
    sec.write_text(TOKEN)
    os.chmod(sec, 0o600)
    p = tmp_path / "hookspool.toml"
    p.write_text(f'''[spool]
home = "{tmp_path / "home"}"

[alerts]
dry = true

[sources.gcal]
module = "hookspool.sources.gcal"
secrets.channel_token = {{ file = "{sec}" }}
expect_every_s = 172800

[destinations."script:gcal_delta"]
argv = ["{sys.executable}", "gcal_delta.py", "--batch"]
state_dir = "{tmp_path / "state"}"
''')
    return str(p)


def test_source_test_cli_with_the_default_loader(tmp_path, capsys):
    cap = os.path.join(sourcekit.CAPTURES, "gcal", "event.http")
    rc = main(["--config", _config(tmp_path), "source", "test", "gcal", "--request", cap])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0 and doc["result"] == "queued", doc


def test_config_check_accepts_the_example(tmp_path):
    rep = hs_config.check(hs_config.load_config(_config(tmp_path)), check_secrets=True)
    assert [e for e in rep.errors if "gcal" in e] == [], rep.errors


# ---- capture generation (maintenance) -----------------------------------------------------------------------------
def write_captures() -> None:
    d = os.path.join(sourcekit.CAPTURES, "gcal")
    os.makedirs(d, exist_ok=True)

    def put(name, meta, hdr, token):
        h = list(hdr.items()) + ([("X-Goog-Channel-Token", token)] if token is not None else [])
        with open(os.path.join(d, name), "wb") as f:
            f.write(P.format_capture(P.RawRequest("POST", "/hooks/gcal", h, b"", meta)))

    m = lambda c, e: {"capture": c, "now": "1800000000", "expect": e}      # noqa: E731
    put("event.http", m("a change notification with the channel token", "queued"), push_headers(), TOKEN)
    put("sync.http", m("the ping Google sends when the channel is created", "ignored"),
        push_headers(state="sync", number="1"), TOKEN)
    put("wrong_token.http", m("a token that is not ours", "unverified"), push_headers(), "not-the-token")
    put("no_token.http", m("no token at all", "unverified"), push_headers(), None)


if __name__ == "__main__":
    if sys.argv[1:] == ["--write"]:
        write_captures()
    else:
        print(__doc__)
