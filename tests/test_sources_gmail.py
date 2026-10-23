"""Gmail Pub/Sub push source (contract C4, T6): the shared kit, the push token, envelope decoding,
quarantine of undecodable pushes, key/dedup/coalescing, and parity of the token check with the bridge
it replaces (``gmail_bridge.py``: ``?token=`` or ``X-Push-Token``, constant-time compare).

Regenerate captures with ``python tests/test_sources_gmail.py --write``.
"""
from __future__ import annotations

import base64
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
from hookspool.plugin import BadPayload, Request, parse_capture  # noqa: E402
from hookspool.sources import gmail  # noqa: E402

TOKEN = "test-gmail-push-token-" + SENTINEL
ACCOUNT = "owner@example.test"
SUB = "projects/test-project/subscriptions/" + SENTINEL


def envelope(address: str = ACCOUNT, history="4242", message_id: str = "m-1", *, pad: bool = True, **extra) -> bytes:
    data = base64.b64encode(json.dumps({"emailAddress": address, "historyId": history}).encode()).decode()
    if not pad:
        data = data.rstrip("=")
    msg = {"data": data, "messageId": message_id, "publishTime": "2026-10-23T10:00:00Z"}
    msg.update(extra)
    return json.dumps({"message": msg, "subscription": SUB}, separators=(",", ":")).encode()


def sign(raw, secrets, now):
    sep = "&" if "?" in raw.target else "?"
    return P.RawRequest(raw.method, f"{raw.target}{sep}token={secrets['push_token']}", list(raw.headers), raw.body,
                        dict(raw.meta))


KIT = Kit(name="gmail", module=gmail, secrets={"push_token": TOKEN}, sign=sign, body_signed=False,
          sample=request("POST", "/hooks/gmail", {"Content-Type": "application/json"}, envelope()))


@pytest.mark.parametrize("case", sourcekit.CASES, ids=sourcekit.CASE_IDS)
def test_kit_matrix(case, tmp_path, clock):
    sourcekit.run(case, KIT, tmp_path, clock)


CAPTURES = sourcekit.captures("gmail")


@pytest.mark.parametrize("path", CAPTURES, ids=[os.path.basename(p) for p in CAPTURES])
def test_captures(path, tmp_path, clock):
    assert sourcekit.run_capture(path, KIT, tmp_path, clock)["source"] == "gmail"


def test_captures_cover_the_outcomes():
    got = {}
    for c in CAPTURES:
        with open(c, "rb") as f:
            got[os.path.basename(c)] = parse_capture(f.read()).meta["expect"]
    assert got.get("event.http") == "queued" and got.get("wrong_token.http") == "unverified", got
    assert got.get("no_token.http") == "unverified", got


@pytest.fixture
def env(tmp_path, clock):
    e = Env(KIT, tmp_path, clock)
    yield e
    e.close()


def post(env, body: bytes, *, token: str | None = TOKEN, header: str | None = None, target="/hooks/gmail"):
    hs = {"Content-Type": "application/json"}
    if header is not None:
        hs["X-Push-Token"] = header
    t = target + (f"?token={token}" if token is not None else "")
    return env.send(request("POST", t, hs, body))


def status(r):
    return r.status, (json.loads(r.body)["status"] if r.body else "")


# ---- the push token ---------------------------------------------------------------------------------------
def test_token_in_the_query_or_in_the_header(env):
    assert status(post(env, envelope(message_id="a"))) == (200, "queued")
    assert status(post(env, envelope(message_id="b"), token=None, header=TOKEN)) == (200, "queued")


def test_wrong_missing_and_empty_tokens_are_401(env):
    for kw in ({"token": "nope"}, {"token": None}, {"token": ""}, {"token": TOKEN[:-1]}, {"token": TOKEN + "x"},
               {"token": None, "header": "nope"}):
        r = post(env, envelope(), **kw)
        assert (r.status, r.body) == (401, b""), kw
    assert env.events() == []


def test_the_query_token_wins_over_the_header_as_in_the_bridge(env):
    assert post(env, envelope(), token="nope", header=TOKEN).status == 401
    assert status(post(env, envelope(message_id="c"), token=TOKEN, header="nope")) == (200, "queued")


def test_the_token_is_not_stored_or_logged(env):
    post(env, envelope())
    env.assert_no_secret_in_db()
    env.assert_clean_logs()
    stored = json.loads(env.events()[0]["headers"] or "{}")
    assert not any(TOKEN in str(v) for v in stored.values()) and "X-Push-Token" not in stored


def test_parity_with_the_bridge_token_rule():
    """gmail_bridge.do_POST: ``tok = ?token or X-Push-Token`` compared in constant time to the token.
    The plugin additionally refuses an empty token (the bridge generated one, so it was never empty)."""
    def old(query, header, token):
        tok = query or header or ""
        return hmac.compare_digest(tok, token)
    cases = [("", ""), (TOKEN, ""), ("", TOKEN), ("x", TOKEN), (TOKEN, "x"), ("", "x"), (TOKEN, TOKEN)]
    for q, h in cases:
        target = "/hooks/gmail" + (f"?token={q}" if q else "")
        req = Request("POST", target, [("X-Push-Token", h)] if h else [], envelope(), now=0)
        assert gmail.verify(req, {"push_token": TOKEN}) is old(q, h, TOKEN), (q, h)
    req = Request("POST", "/hooks/gmail", [], envelope(), now=0)
    assert gmail.verify(req, {"push_token": ""}) is False
    assert hmac.compare_digest("", "") is True            # what the bridge would have done with an empty token


# ---- payload decoding -----------------------------------------------------------------------------------------
def classify(body: bytes):
    return P.Source("gmail", gmail, {}).classify(Request("POST", "/hooks/gmail", [], body, now=0))


def test_the_envelope_is_normalised():
    c = classify(envelope("Owner@Example.TEST", 99, "m-7"))
    assert c.payload == {"emailAddress": "owner@example.test", "historyId": "99", "messageId": "m-7",
                         "publishTime": "2026-10-23T10:00:00Z", "subscription": SUB}
    assert c.key == "owner@example.test" and c.dedup_id == "m-7" and c.event_type == "history"
    assert c.attrs == {"account": "owner@example.test"} and c.default_action == "script:gmail_delta"


def test_unpadded_base64_decodes():
    assert classify(envelope(pad=False)).key == ACCOUNT


def test_a_missing_message_id_is_not_a_dedup_id():
    body = json.loads(envelope())
    del body["message"]["messageId"]
    assert classify(json.dumps(body).encode()).dedup_id is None


def b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode() if not isinstance(obj, bytes) else obj).decode()


@pytest.mark.parametrize("body", [
    b"[]", b"{}", b'{"message": 5}', b'{"message": {}}', b'{"message": {"data": ""}}',
    json.dumps({"message": {"data": "!!!not base64!!!"}}).encode(),
    json.dumps({"message": {"data": b64(b"\xff\xfe")}}).encode(),
    json.dumps({"message": {"data": b64(b"not json")}}).encode(),
    json.dumps({"message": {"data": b64([1, 2])}}).encode(),
    json.dumps({"message": {"data": b64({"historyId": 1})}}).encode(),
    json.dumps({"message": {"data": b64({"emailAddress": "nobody", "historyId": 1})}}).encode(),
    json.dumps({"message": {"data": b64({"emailAddress": ACCOUNT})}}).encode(),
    json.dumps({"message": {"data": b64({"emailAddress": ACCOUNT, "historyId": True})}}).encode(),
    b"not json",
], ids=lambda b: b[:30].decode("latin-1"))
def test_an_undecodable_push_is_a_bad_payload(body):
    with pytest.raises(BadPayload):
        classify(body)


def test_an_undecodable_push_is_quarantined_not_dropped(env):
    r = post(env, b'{"message": {"data": "!!"}}')
    assert (r.status, json.loads(r.body)["status"]) == (202, "quarantined")
    b = env.batches()
    assert len(b) == 1 and b[0]["destination"] == "quarantine" and b[0]["state"] == "dead"


# ---- coalescing and dedup --------------------------------------------------------------------------------------------
def test_pushes_for_one_mailbox_coalesce_and_other_mailboxes_do_not(env):
    for i in range(3):
        assert status(post(env, envelope(history=100 + i, message_id=f"m{i}"))) == (200, "queued")
    assert status(post(env, envelope("other@example.test", 5, "o1"))) == (200, "queued")
    rows = {r["key"]: r for r in env.batches()}
    assert set(rows) == {ACCOUNT, "other@example.test"}
    assert rows[ACCOUNT]["count"] == 3 and rows["other@example.test"]["count"] == 1
    assert rows[ACCOUNT]["destination"] == "script:gmail_delta"
    assert (rows[ACCOUNT]["quiet_s"], rows[ACCOUNT]["max_s"]) == (30.0, 120.0)        # the mail span


def test_a_redelivered_pubsub_message_is_a_duplicate(env):
    assert status(post(env, envelope(message_id="same"))) == (200, "queued")
    assert status(post(env, envelope(message_id="same"))) == (200, "duplicate")
    assert len(env.events()) == 1


def test_a_new_message_id_for_the_same_change_joins_the_batch(env):
    """ARCHITECTURE section 7: a retry after the intake died after COMMIT carries a NEW messageId; it
    becomes a second event in the batch and the delta script reconciles (it reads history)."""
    post(env, envelope(message_id="first"))
    post(env, envelope(message_id="retry"))
    assert [b["count"] for b in env.batches()] == [2]


def test_declarations():
    assert (gmail.span, gmail.sender_retries, gmail.action) == ("mail", True, "script:gmail_delta")
    assert gmail.auth_headers == ("X-Push-Token",) and gmail.keep_headers == ()
    assert gmail.secrets == ("push_token",)


# ---- config --------------------------------------------------------------------------------------------------------------
def _config(tmp_path) -> str:
    sec = tmp_path / "push-token"
    sec.write_text(TOKEN)
    os.chmod(sec, 0o600)
    state = tmp_path / "state"
    p = tmp_path / "hookspool.toml"
    p.write_text(f'''[spool]
home = "{tmp_path / "home"}"

[alerts]
dry = true

[sources.gmail]
module = "hookspool.sources.gmail"
secrets.push_token = {{ file = "{sec}" }}

[destinations."script:gmail_delta"]
argv = ["{sys.executable}", "gmail_delta.py", "--batch"]
state_dir = "{state}"
''')
    return str(p)


def test_source_test_cli_with_the_default_loader(tmp_path, capsys):
    cap = os.path.join(sourcekit.CAPTURES, "gmail", "event.http")
    rc = main(["--config", _config(tmp_path), "source", "test", "gmail", "--request", cap])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0 and doc["result"] == "queued", doc


def test_config_check_accepts_the_example(tmp_path):
    cfg = hs_config.load_config(_config(tmp_path))
    rep = hs_config.check(cfg, check_secrets=True)
    assert [e for e in rep.errors if "gmail" in e] == [], rep.errors


# ---- capture generation (maintenance) -------------------------------------------------------------------------------
def write_captures() -> None:
    d = os.path.join(sourcekit.CAPTURES, "gmail")
    os.makedirs(d, exist_ok=True)
    hdr = [("Content-Type", "application/json"), ("User-Agent", "APIs-Google; (+https://developers.google.com/webmasters/APIs-Google.html)")]

    def put(name, meta, body, token):
        target = "/hooks/gmail" + (f"?token={token}" if token is not None else "")
        with open(os.path.join(d, name), "wb") as f:
            f.write(P.format_capture(P.RawRequest("POST", target, list(hdr), body, meta)))

    put("event.http", {"capture": "history notification with the push token in the URL", "now": "1800000000",
                       "expect": "queued"}, envelope(), TOKEN)
    put("wrong_token.http", {"capture": "a token that is not ours", "now": "1800000000", "expect": "unverified"},
        envelope(), "not-the-token")
    put("no_token.http", {"capture": "no token at all", "now": "1800000000", "expect": "unverified"}, envelope(), None)


if __name__ == "__main__":
    if sys.argv[1:] == ["--write"]:
        write_captures()
    else:
        print(__doc__)
