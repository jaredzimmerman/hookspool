"""Shared destination wire code: Standard Webhooks signing, the HTTP transport's time bounds,
and the status -> outcome table (C3, ARCHITECTURE §6.4 and §6.5)."""
import base64
import hashlib
import hmac
import threading
import time

import pytest

from _httpstub import Stub, make_ctx, verify_legacy, verify_standard_webhooks
from hookspool.destinations import wire

# The Standard Webhooks specification's published example.
SPEC_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
SPEC_ID, SPEC_TS, SPEC_BODY = "msg_p5jXN8AQM9LWM0D4loKWxJek", 1614265330, b'{"test": 2432232314}'
SPEC_SIG = "g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


# ---- signing -----------------------------------------------------------------------------------
def test_signature_matches_the_specification_vector():
    assert wire.sign(wire.signing_key(SPEC_SECRET), SPEC_ID, SPEC_TS, SPEC_BODY) == SPEC_SIG


def test_whsec_secret_is_base64_decoded_and_raw_secret_is_not():
    assert wire.signing_key(SPEC_SECRET) == base64.b64decode(SPEC_SECRET[6:])
    assert wire.signing_key("plain-secret") == b"plain-secret"
    assert wire.signing_key("whsec_" + base64.b64encode(b"k").decode()) == b"k"


@pytest.mark.parametrize("secret", ["", "whsec_", "whsec_!!!not-base64", "whsec_AAAA"])
def test_unusable_secrets_are_refused_without_echoing_them(secret):
    with pytest.raises(wire.BadSecret) as e:
        wire.signing_key(secret)
    assert secret.removeprefix("whsec_") == "" or secret.removeprefix("whsec_") not in str(e.value)


def test_headers_verify_independently_and_carry_the_batch_identity():
    ctx = make_ctx(batch_id=42, attempt=3, now=1_800_000_123.9)
    body = b'{"a":1}'
    h = wire.delivery_headers(["s3cret"], "hs_x_42", ctx, body)
    assert verify_standard_webhooks(h, body, "s3cret", now=1_800_000_123)
    assert h["webhook-id"] == "hs_x_42" and h["webhook-timestamp"] == "1800000123"
    assert h["webhook-signature"].startswith("v1,") and " " not in h["webhook-signature"]
    assert h["X-Hookspool-Batch"] == "42" and h["X-Hookspool-Attempt"] == "3"
    assert h["Content-Type"] == "application/json" and h["User-Agent"].startswith("hookspool/")
    assert "X-Hub-Signature-256" not in h
    # a changed body, id or secret fails the independent check
    assert not verify_standard_webhooks(h, body + b" ", "s3cret", now=1_800_000_123)
    assert not verify_standard_webhooks(h, body, "other", now=1_800_000_123)
    assert not verify_standard_webhooks({**h, "webhook-id": "hs_x_43"}, body, "s3cret", now=1_800_000_123)


def test_legacy_signature_uses_the_raw_secret_even_for_whsec():
    ctx = make_ctx()
    body = b"{}"
    h = wire.delivery_headers([SPEC_SECRET], "hs_x_7", ctx, body, legacy=True)
    assert verify_standard_webhooks(h, body, SPEC_SECRET, now=1_800_000_000)
    assert verify_legacy(h, body, SPEC_SECRET)
    assert h["X-Hub-Signature-256"] == "sha256=" + hmac.new(SPEC_SECRET.encode(), body, hashlib.sha256).hexdigest()


def test_rotation_emits_two_signatures_each_valid_alone():
    ctx = make_ctx()
    body = b'{"x":2}'
    h = wire.delivery_headers(["new-secret", "old-secret"], "hs_x_7", ctx, body)
    parts = h["webhook-signature"].split(" ")
    assert len(parts) == 2 and all(p.startswith("v1,") for p in parts)
    assert verify_standard_webhooks(h, body, "new-secret", now=1_800_000_000)
    assert verify_standard_webhooks(h, body, "old-secret", now=1_800_000_000)
    assert not verify_standard_webhooks(h, body, "third", now=1_800_000_000)


def test_timestamp_is_the_dispatcher_clock_so_a_retry_re_signs_fresh():
    a = wire.delivery_headers(["s"], "id", make_ctx(now=1000.0), b"b")
    b = wire.delivery_headers(["s"], "id", make_ctx(now=1015.0), b"b")
    assert a["webhook-timestamp"] == "1000" and b["webhook-timestamp"] == "1015"
    assert a["webhook-signature"] != b["webhook-signature"]


# ---- status table (§6.5) -----------------------------------------------------------------------
def _cls(code, headers=None, *, redirect_permanent=True, now=1000.0):
    return wire.classify_status(wire.Response(code, headers or {}), now, redirect_permanent=redirect_permanent)


@pytest.mark.parametrize("code,kind,klass", [
    (400, "dead", "permanent"), (413, "dead", "permanent"), (415, "dead", "permanent"), (422, "dead", "permanent"),
    (401, "retry", "config"), (403, "retry", "config"), (404, "retry", "config"), (405, "retry", "config"),
    (410, "retry", "config"), (409, "retry", "config"), (418, "retry", "config"),
    (408, "retry", "transport"), (500, "retry", "transport"), (502, "retry", "transport"),
    (503, "retry", "transport"), (504, "retry", "transport"), (599, "retry", "transport"),
    (429, "retry", "throttle"), (301, "dead", "permanent"), (302, "dead", "permanent"),
    (307, "dead", "permanent"), (308, "dead", "permanent"), (100, "retry", "transport"),
])
def test_status_table(code, kind, klass):
    o = _cls(code)
    assert (o.kind, o.klass, o.status) == (kind, klass, code)


def test_2xx_is_ok():
    for code in (200, 201, 202, 204):
        assert _cls(code).kind == "ok"


def test_redirect_is_config_where_the_caller_says_so():
    o = _cls(301, redirect_permanent=False)
    assert (o.kind, o.klass) == ("retry", "config")


def test_503_with_retry_after_is_throttle_and_without_is_transport():
    assert (_cls(503, {"retry-after": "30"}).klass, _cls(503, {"retry-after": "30"}).retry_after_s) == ("throttle", 30.0)
    assert _cls(503).klass == "transport"
    assert _cls(503, {"retry-after": "garbage"}).klass == "transport"


def test_retry_after_forms():
    assert wire.parse_retry_after("120", 0) == 120.0
    assert wire.parse_retry_after(" 7 ", 0) == 7.0
    assert wire.parse_retry_after("-5", 0) == 0.0
    date = "Thu, 01 Jan 1970 00:10:00 GMT"          # epoch + 600
    assert wire.parse_retry_after(date, 100.0) == 500.0
    assert wire.parse_retry_after(date, 900.0) == 0.0
    for bad in (None, "", "soon", "nan", "inf"):
        assert wire.parse_retry_after(bad, 0) is None
    assert _cls(429, {"retry-after": "45"}).retry_after_s == 45.0
    assert _cls(429).retry_after_s is None


# ---- transport: bounded in time, no redirects ----------------------------------------------------
@pytest.fixture
def stub():
    made = []

    def start(**kw):
        s = Stub(**kw)
        made.append(s)
        return s
    yield start
    for s in made:
        s.close()


def test_post_returns_status_headers_and_a_capped_body(stub):
    s = stub()
    s.respond({"status": 202, "headers": {"X-Test": "yes"}, "body": b"x" * (wire.READ_LIMIT + 5000)})
    r = wire.post(s.url + "/a/b?c=1", {"Content-Type": "application/json"}, b'{"k":1}', 5)
    assert r.status == 202 and r.headers["x-test"] == "yes" and len(r.body) == wire.READ_LIMIT
    assert s.requests[0]["path"] == "/a/b?c=1" and s.requests[0]["body"] == b'{"k":1}'


def test_post_never_follows_a_redirect(stub):
    target = stub()
    s = stub()
    s.respond({"status": 307, "headers": {"Location": target.url + "/elsewhere"}})
    r = wire.post(s.url + "/x", {}, b"{}", 5)
    assert r.status == 307 and target.requests == []


def test_post_refused_is_unreachable():
    s = Stub()
    url = s.url
    s.close()
    with pytest.raises(wire.NetError) as e:
        wire.post(url, {}, b"{}", 5)
    assert e.value.kind == "unreachable"


def test_post_without_budget_does_not_send(stub):
    s = stub()
    with pytest.raises(wire.NetError) as e:
        wire.post(s.url, {}, b"{}", 0)
    assert e.value.kind == "timeout" and s.requests == []


def test_a_request_that_connects_after_the_watchdog_never_sends(stub, monkeypatch):
    import http.client
    s = stub()
    real = http.client.HTTPConnection.connect

    def slow_connect(self):         # stands in for a DNS lookup that outlives the budget
        time.sleep(0.6)
        return real(self)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", slow_connect)
    with pytest.raises(wire.NetError) as e:
        wire.post(s.url, {}, b"{}", 0.2)
    assert e.value.kind == "timeout"
    time.sleep(0.8)
    assert s.requests == []


def test_post_bad_url():
    for u in ("ftp://x/y", "http://", "nonsense"):
        with pytest.raises(wire.NetError) as e:
            wire.post(u, {}, b"{}", 1)
        assert e.value.kind == "protocol"


def test_post_stalled_receiver_returns_at_the_budget(stub):
    s = stub()
    hold = threading.Event()
    s.respond({"status": 200, "hold": hold})
    t0 = time.monotonic()
    with pytest.raises(wire.NetError) as e:
        wire.post(s.url, {}, b"{}", 0.4)
    took = time.monotonic() - t0
    hold.set()
    assert e.value.kind == "timeout" and 0.3 <= took < 2.0


def test_post_dripping_receiver_cannot_outlive_the_budget(stub):
    """A response that trickles a byte at a time never trips the per-read socket timeout;
    the total budget must still end it."""
    s = stub()
    s.respond({"status": 200, "drip": 0.1})
    t0 = time.monotonic()
    with pytest.raises(wire.NetError) as e:
        wire.post(s.url, {}, b"{}", 0.5)
    assert e.value.kind == "timeout" and time.monotonic() - t0 < 2.0


def test_post_hung_connect_is_cut_by_the_watchdog(monkeypatch):
    """Even when connect() itself never returns (a black-holed address, a stuck resolver), post
    returns at the budget."""
    import http.client
    release = threading.Event()

    def hang(self):
        release.wait(30)
        raise OSError("released")
    monkeypatch.setattr(http.client.HTTPConnection, "connect", hang)
    t0 = time.monotonic()
    with pytest.raises(wire.NetError) as e:
        wire.post("http://192.0.2.1:9/", {}, b"{}", 0.4)
    release.set()
    assert e.value.kind == "timeout" and time.monotonic() - t0 < 2.0


def test_net_outcome_classes():
    assert wire.net_outcome(wire.NetError("timeout"), timed_out_reason="deadline").reason == "deadline"
    assert wire.net_outcome(wire.NetError("timeout"), timed_out_reason="timeout").klass == "transport"
    assert wire.net_outcome(wire.NetError("unreachable", "ConnectionRefusedError"),
                            timed_out_reason="timeout").klass == "transport"
    assert wire.net_outcome(wire.NetError("protocol", "BadStatusLine"), timed_out_reason="timeout").klass == "transport"
    o = wire.net_outcome(wire.NetError("tls", "X"), timed_out_reason="timeout")
    assert (o.kind, o.klass) == ("retry", "config")
