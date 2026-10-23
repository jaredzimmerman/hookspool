"""Zoom source (contract C4, T6): the shared kit, the signature (``v0=`` + hex, as Zoom documents), the
URL-validation handshake that is answered only after ``verify``, key and dedup.

Verification parity with the bridge it replaces (``vps-src/scripts/zoom_bridge.py``, absent in a public
checkout): the bridge's check could never succeed (it compares ``v0=<digest>`` to an unprefixed base64
digest), so there is nothing to stay compatible with. The reference function below is the bridge's,
copied without its secrets; the tests pin that it accepts nothing, that the plugin accepts exactly the
documented scheme, and, when the real file is present, run the real function over the same grid.

Regenerate signed captures with ``python tests/test_sources_zoom.py --write``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sourcekit  # noqa: E402
import vpssrc  # noqa: E402
from sourcekit import SENTINEL, Env, Kit, request  # noqa: E402

from hookspool import plugin as P  # noqa: E402
from hookspool.plugin import BadPayload, Request, parse_capture  # noqa: E402
from hookspool.sources import zoom  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRET = "test-zoom-secret-token-" + SENTINEL
NOW = 1_800_000_000.0
BODY = json.dumps({"event": "meeting.ended", "event_ts": 1799999999000, "payload": {
    "account_id": "acct-1", "object": {"uuid": "uuid-1==", "id": 111, "topic": SENTINEL}}},
    separators=(",", ":")).encode()
VALIDATION = json.dumps({"event": "endpoint.url_validation", "payload": {"plainToken": "plain-" + SENTINEL}},
                        separators=(",", ":")).encode()


def mac(body: bytes, ts, secret: str = SECRET) -> bytes:
    return hmac.new(secret.encode(), b"v0:" + str(ts).encode() + b":" + body, hashlib.sha256).digest()


def sign(raw, secrets, now, *, enc: str = "hex"):
    ts = int(now)
    m = mac(raw.body, ts, secrets["webhook_secret"])
    sig = m.hex() if enc == "hex" else base64.b64encode(m).decode()
    return raw.set("x-zm-request-timestamp", str(ts)).set("x-zm-signature", "v0=" + sig)


KIT = Kit(
    name="zoom", module=zoom, secrets={"webhook_secret": SECRET}, sign=sign, timestamped=True,
    sample=request("POST", "/hooks/zoom", {"Content-Type": "application/json"}, BODY),
    handshake=request("POST", "/hooks/zoom", {"Content-Type": "application/json"}, VALIDATION),
)


@pytest.mark.parametrize("case", sourcekit.CASES, ids=sourcekit.CASE_IDS)
def test_kit_matrix(case, tmp_path, clock):
    sourcekit.run(case, KIT, tmp_path, clock)


CAPTURES = sourcekit.captures("zoom")


@pytest.mark.parametrize("path", CAPTURES, ids=[os.path.basename(p) for p in CAPTURES])
def test_captures(path, tmp_path, clock):
    out = sourcekit.run_capture(path, KIT, tmp_path, clock)
    assert out["source"] == "zoom"


def test_captures_cover_the_outcomes():
    got = {}
    for c in CAPTURES:
        with open(c, "rb") as f:
            got[os.path.basename(c)] = parse_capture(f.read()).meta["expect"]
    assert got.get("event.http") == "queued", got
    assert got.get("url_validation.http") == "challenge", got
    assert got.get("url_validation_unsigned.http") == "unverified", got
    assert "unverified" in got.values() and len(got) >= 5, got


@pytest.fixture
def env(tmp_path, clock):
    e = Env(KIT, tmp_path, clock)
    yield e
    e.close()


# ---- the validation handshake ----------------------------------------------------------------------------
def test_validation_answers_a_signed_request_with_the_documented_digest(env):
    r = env.send(env.signed(KIT.handshake))
    assert r.status == 200 and r.headers["Content-Type"] == "application/json"
    doc = json.loads(r.body)
    assert doc == {"plainToken": "plain-" + SENTINEL,
                   "encryptedToken": hmac.new(SECRET.encode(), ("plain-" + SENTINEL).encode(),
                                              hashlib.sha256).hexdigest()}
    assert env.batches() == [] and env.events() == []


def test_validation_unsigned_is_refused_with_no_token_in_the_body(env):
    r = env.send(KIT.handshake)
    assert (r.status, r.body) == (401, b"")
    assert "challenge" not in env.spy.calls
    assert env.batches() == []


@pytest.mark.parametrize("what", ["wrong-secret", "tampered", "stale", "future", "no-timestamp", "no-signature",
                                  "wrong-prefix"])
def test_validation_with_a_bad_signature_is_refused(env, what):
    good = env.signed(KIT.handshake)
    if what == "wrong-secret":
        bad = sign(KIT.handshake, {"webhook_secret": "other-" + SECRET}, env.clock())
    elif what == "tampered":
        bad = good.with_body(good.body.replace(b"plain-", b"plaiX-"))
    elif what == "stale":
        bad = sign(KIT.handshake, KIT.secrets, env.clock() - 301)
    elif what == "future":
        bad = sign(KIT.handshake, KIT.secrets, env.clock() + 301)
    elif what == "no-timestamp":
        bad = good.set("x-zm-request-timestamp", None)
    elif what == "no-signature":
        bad = good.set("x-zm-signature", None)
    else:
        bad = good.set("x-zm-signature", "v1=" + good.header("x-zm-signature")[3:])
    r = env.send(bad)
    assert (r.status, r.body) == (401, b""), what
    assert "challenge" not in env.spy.calls and env.batches() == []


def test_validation_without_a_plain_token_is_a_400_after_verify(env):
    body = json.dumps({"event": "endpoint.url_validation", "payload": {}}).encode()
    r = env.send(env.signed(KIT.handshake.with_body(body)))
    assert r.status == 400 and b"plainToken" in r.body and env.batches() == []


def test_only_post_validation_events_are_challenges(env):
    assert zoom.challenge(Request("POST", "/hooks/zoom", [], BODY), {"webhook_secret": SECRET}) is None
    assert zoom.challenge(Request("GET", "/hooks/zoom", [], VALIDATION), {"webhook_secret": SECRET}) is None
    assert zoom.challenge(Request("POST", "/hooks/zoom", [], b"not json"), {"webhook_secret": SECRET}) is None


# ---- signature: hex, base64 and the old bridge ------------------------------------------------------------
def test_the_documented_hex_digest_verifies_and_base64_does_not(env):
    assert env.send(sign(KIT.sample, KIT.secrets, env.clock())).status == 200
    assert env.send(sign(KIT.sample, KIT.secrets, env.clock(), enc="base64")).status == 401


def test_a_digest_of_another_message_does_not_verify(env):
    ts = int(env.clock())
    bad = KIT.sample.set("x-zm-request-timestamp", str(ts)).set(
        "x-zm-signature", "v0=" + hmac.new(SECRET.encode(), b"v0:" + str(ts).encode() + b"." + BODY,
                                           hashlib.sha256).hexdigest())
    assert env.send(bad).status == 401


def test_the_timestamp_edge_is_inclusive_and_two_sided(env):
    for dt, ok in ((-300, True), (300, True), (-301, False), (301, False)):
        r = env.send(sign(KIT.sample, KIT.secrets, env.clock() + dt))
        assert (r.status == 200) is ok, dt


def _old_verify(body: bytes, timestamp: str, provided: str, secret: str, now: float) -> bool:
    """zoom_bridge.verify_zoom_signature, verbatim apart from the injected clock."""
    if not (timestamp and provided and secret):
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(now - ts) > 300:
        return False
    if not provided.startswith("v0="):
        return False
    message = b"v0:" + timestamp.encode() + b":" + body
    expected = base64.b64encode(hmac.new(secret.encode(), message, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(provided, expected)


def _grid():
    for dt in (-301, -300, -1, 0, 1, 300, 301):
        ts = int(NOW) + dt
        m = mac(BODY, ts)
        other = mac(BODY + b" ", ts)
        for label, sig in (("hex", "v0=" + m.hex()), ("b64", "v0=" + base64.b64encode(m).decode()),
                           ("hex-other", "v0=" + other.hex()), ("b64-other", "v0=" + base64.b64encode(other).decode()),
                           ("hex-upper", "v0=" + m.hex().upper()), ("no-prefix", m.hex()), ("v1", "v1=" + m.hex()),
                           ("empty", ""), ("short", "v0=")):
            yield dt, label, str(ts), sig
    yield 0, "bad-ts", "abc", "v0=" + mac(BODY, 0).hex()
    yield 0, "empty-ts", "", "v0=" + mac(BODY, 0).hex()


def _new_verify(ts: str, sig: str, secret: str = SECRET) -> bool:
    req = Request("POST", "/hooks/zoom", [("x-zm-request-timestamp", ts), ("x-zm-signature", sig)], BODY, now=NOW)
    return zoom.verify(req, {"webhook_secret": secret}) is True


def test_parity_with_the_bridge_verification():
    """The bridge accepted nothing, whatever the grid held; the plugin accepts exactly the hex digests
    of the right message inside the window."""
    accepted = []
    for dt, label, ts, sig in _grid():
        assert not _old_verify(BODY, ts, sig, SECRET, NOW), f"the bridge reference accepted {label} at {dt:+d}s"
        if _new_verify(ts, sig):
            accepted.append((label, dt))
    assert sorted(accepted) == sorted((lab, dt) for lab in ("hex", "hex-upper") for dt in (-300, -1, 0, 1, 300))
    assert not _new_verify(str(int(NOW)), "v0=" + mac(BODY, int(NOW)).hex(), secret="")        # unset secret


def test_parity_with_the_real_bridge_file_when_present(monkeypatch):
    bridge = vpssrc.load("zoom_bridge.py")
    monkeypatch.setattr(time, "time", lambda: NOW)
    for dt, label, ts, sig in _grid():
        assert bridge.verify_zoom_signature(BODY, ts, sig, SECRET) is False, (label, dt)


# ---- classification ----------------------------------------------------------------------------------------
def _classify(body: dict | bytes):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return P.Source("zoom", zoom, {}).classify(Request("POST", "/hooks/zoom", [], raw, now=NOW))


def test_meeting_events_key_on_the_uuid_and_dedup_on_event_ts_and_uuid():
    c = _classify(BODY)
    assert c.key == "uuid-1==" and c.dedup_id == "meeting.ended:1799999999000:uuid-1=="
    assert c.event_type == "meeting.ended" and c.attrs == {"event": "meeting.ended", "kind": "meeting"}
    assert c.default_action == "hermes:zoom"


def test_key_falls_back_to_id_then_account_then_event():
    base = {"event": "user.updated", "event_ts": 5}
    assert _classify(dict(base, payload={"object": {"id": "u-9"}})).key == "u-9"
    assert _classify(dict(base, payload={"account_id": "A1", "object": {}})).key == "account:A1"
    assert _classify(dict(base, payload={})).key == "event:user.updated"
    assert _classify({"hello": 1}).key == "event:unknown"
    assert _classify(dict(base, payload={"object": {"id": 7}})).dedup_id == "user.updated:5:7"
    assert _classify(dict(base, payload={})).dedup_id is None
    assert _classify({"event": "x", "payload": {"object": {"uuid": "u"}}}).dedup_id is None      # no event_ts


def test_same_meeting_events_share_a_key_and_distinct_meetings_do_not():
    a = _classify(BODY)
    b = _classify(json.loads(BODY) | {"event": "meeting.started", "event_ts": 1})
    c = _classify({"event": "meeting.ended", "event_ts": 1, "payload": {"object": {"uuid": "other"}}})
    assert a.key == b.key != c.key


def test_a_non_json_body_is_a_bad_payload_that_the_intake_quarantines():
    with pytest.raises(BadPayload):
        _classify(b"not json")


def test_the_immediate_span_and_declarations():
    assert zoom.span == "immediate" and zoom.sender_retries is True
    assert zoom.auth_headers == ("x-zm-signature",) and zoom.keep_headers == ()
    assert not getattr(zoom, "challenge_unauthenticated", False), "Zoom validation is verified first"


# ---- config -----------------------------------------------------------------------------------------------------
def _config(tmp_path) -> str:
    sec = tmp_path / "zoom-secret"
    sec.write_text(SECRET)
    os.chmod(sec, 0o600)
    p = tmp_path / "hookspool.toml"
    p.write_text(f'''[spool]
home = "{tmp_path / "home"}"

[alerts]
dry = true

[sources.zoom]
module = "hookspool.sources.zoom"
secrets.webhook_secret = {{ file = "{sec}" }}
''')
    return str(p)


@pytest.mark.parametrize("name,expect,rc", [("event.http", "queued", 0), ("url_validation.http", "challenge", 0),
                                            ("url_validation_unsigned.http", "unverified", 1)])
def test_source_test_cli_with_the_default_loader(tmp_path, capsys, name, expect, rc):
    from hookspool.__main__ import main
    code = main(["--config", _config(tmp_path), "source", "test", "zoom", "--request",
                 os.path.join(sourcekit.capture_dir("zoom"), name)])
    out = capsys.readouterr().out
    assert json.loads(out)["result"] == expect and code == rc and SECRET not in out and SENTINEL not in out


def test_config_check_accepts_the_example(tmp_path):
    from hookspool import config as hs_config
    rep = hs_config.check(hs_config.load_config(_config(tmp_path)), check_secrets=True)
    assert [e for e in rep.errors if "zoom" in e] == [], rep.errors


# ---- capture generation (maintenance) ------------------------------------------------------------------------
def write_captures() -> None:
    d = sourcekit.capture_dir("zoom")
    os.makedirs(d, exist_ok=True)
    hdr = [("Content-Type", "application/json"), ("User-Agent", "Zoom Marketplace/1.0a")]

    def put(name, meta, body, signed_at=NOW, secret=SECRET, sig_of=None, sign_it=True):
        raw = P.RawRequest("POST", "/hooks/zoom", list(hdr), body, meta)
        if sign_it:
            basis = P.RawRequest("POST", "/hooks/zoom", list(hdr), sig_of if sig_of is not None else body)
            s = sign(basis, {"webhook_secret": secret}, signed_at)
            raw = P.RawRequest("POST", "/hooks/zoom", s.headers, body, meta)
        with open(os.path.join(d, name), "wb") as f:
            f.write(P.format_capture(raw))

    m = lambda c, e: {"capture": c, "now": str(int(NOW)), "expect": e}      # noqa: E731
    put("event.http", m("meeting.ended signed at '# now'", "queued"), BODY)
    put("url_validation.http", m("verified endpoint.url_validation", "challenge"), VALIDATION)
    put("url_validation_unsigned.http", m("validation request without a signature: refused", "unverified"),
        VALIDATION, sign_it=False)
    put("tampered.http", m("body changed after signing", "unverified"), BODY.replace(b"uuid-1", b"uuid-2"), sig_of=BODY)
    put("stale.http", m("signed 301 s before '# now'", "unverified"), BODY, signed_at=NOW - 301)


if __name__ == "__main__":
    if sys.argv[1:] == ["--write"]:
        write_captures()
    else:
        print(__doc__)
