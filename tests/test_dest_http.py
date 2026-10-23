"""``http`` destination (C3, §6.4): https required, verified TLS, no redirects, secret refs
read fresh, rotation signatures, per-batch ids, static headers, time bounds."""
import threading
import time

import pytest

from _httpstub import Stub, doc_for, encode, make_cert, make_ctx, verify_standard_webhooks
from conftest import SENTINEL
from hookspool.destinations import build
from hookspool.destinations.http import HttpDestination


def write(path, text):
    path.write_text(text)
    return {"file": str(path)}


@pytest.fixture
def stubs():
    made = []

    def start(**kw):
        s = Stub(**kw)
        made.append(s)
        return s
    yield start
    for s in made:
        s.close()


@pytest.fixture
def secret_file(tmp_path):
    return write(tmp_path / "audit.secret", "audit-secret-1\n")


def dest(make_config, name="audit", **spec):
    spec.setdefault("type", "http")
    cfg = make_config(destinations={name: spec})
    d = build(cfg, name)
    assert isinstance(d, HttpDestination), d
    return d


def go(d, doc=None, delivery_id="hs_inst_7", ctx=None):
    doc = doc or doc_for()
    return d.deliver(doc, encode(doc), delivery_id, ctx or make_ctx(wall=True))


# ---- type properties -----------------------------------------------------------------------------
def test_type_properties(make_config, secret_file):
    d = dest(make_config, url="https://example.invalid/h", secret=secret_file)
    assert d.idempotency == "per-batch" and d.max_inflight == 4
    assert d.max_body_bytes == 1048576 and d.timeout_s == 30.0
    d = dest(make_config, url="https://example.invalid/h", secret=secret_file, max_body_bytes=2000, timeout_s=7,
             max_inflight=1)
    assert (d.max_body_bytes, d.timeout_s, d.max_inflight) == (2000, 7.0, 1)


# ---- plain http requires allow_insecure -----------------------------------------------------------
def test_http_url_is_refused_without_allow_insecure(make_config, stubs, secret_file):
    s = stubs()
    d = dest(make_config, url=s.url + "/h", secret=secret_file)
    out = go(d)
    assert (out.kind, out.klass) == ("retry", "config") and "allow_insecure" in out.reason
    assert s.requests == []
    assert any("allow_insecure" in p for p in d.check())


def test_allow_insecure_sends_signed_request(make_config, stubs, secret_file):
    s = stubs()
    d = dest(make_config, url=s.url + "/h?x=1", secret=secret_file, allow_insecure=True)
    doc = doc_for(4)
    out = d.deliver(doc, encode(doc), "hs_inst_7", make_ctx(batch_id=7, attempt=3, wall=True))
    assert out.kind == "ok" and out.status == 200
    r = s.requests[0]
    assert r["path"] == "/h?x=1" and r["body"] == encode(doc)
    assert verify_standard_webhooks(r["headers"], r["body"], "audit-secret-1", now=time.time())
    assert r["headers"]["X-Hookspool-Batch"] == "7" and r["headers"]["X-Hookspool-Attempt"] == "3"
    assert "X-Hub-Signature-256" not in r["headers"] and "X-GitHub-Event" not in r["headers"]
    assert d.check() == []


# ---- TLS ---------------------------------------------------------------------------------------
def test_tls_verified_against_a_trusted_certificate(make_config, stubs, tmp_path, monkeypatch, secret_file):
    cert, key = make_cert(tmp_path)
    s = stubs(tls=(cert, key))
    monkeypatch.setenv("SSL_CERT_FILE", cert)
    d = dest(make_config, url=s.url + "/h", secret=secret_file)
    out = go(d)
    assert out.kind == "ok", out
    assert verify_standard_webhooks(s.requests[0]["headers"], s.requests[0]["body"], "audit-secret-1",
                                    now=time.time())


def test_untrusted_certificate_is_refused_and_nothing_is_sent(make_config, stubs, tmp_path, monkeypatch, secret_file):
    cert, key = make_cert(tmp_path)
    other, _ = make_cert(tmp_path, name="other")
    s = stubs(tls=(cert, key))
    monkeypatch.setenv("SSL_CERT_FILE", other)          # trusts a different certificate
    d = dest(make_config, url=s.url + "/h", secret=secret_file)
    out = go(d)
    assert (out.kind, out.klass) == ("retry", "config") and "certificate" in out.reason
    assert s.requests == []


def test_certificate_for_another_host_is_refused(make_config, stubs, tmp_path, monkeypatch, secret_file):
    cert, key = make_cert(tmp_path, ip="10.9.9.9", dns="elsewhere.test")     # not valid for 127.0.0.1
    s = stubs(tls=(cert, key))
    monkeypatch.setenv("SSL_CERT_FILE", cert)
    d = dest(make_config, url=s.url + "/h", secret=secret_file)
    out = go(d)
    assert out.klass == "config" and s.requests == []


def test_tls_context_is_the_verifying_default():
    import ssl
    from hookspool.destinations import wire
    c = wire.tls_context()
    assert c.verify_mode == ssl.CERT_REQUIRED and c.check_hostname


# ---- redirects ----------------------------------------------------------------------------------
@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_is_permanent_and_never_followed(make_config, stubs, secret_file, code):
    target = stubs()
    s = stubs()
    s.respond({"status": code, "headers": {"Location": target.url + "/elsewhere"}})
    d = dest(make_config, url=s.url + "/h", secret=secret_file, allow_insecure=True)
    out = go(d)
    assert (out.kind, out.klass, out.status) == ("dead", "permanent", code)
    assert target.requests == [] and len(s.requests) == 1


# ---- statuses -------------------------------------------------------------------------------------
@pytest.mark.parametrize("code,kind,klass", [
    (200, "ok", None), (204, "ok", None), (400, "dead", "permanent"), (413, "dead", "permanent"),
    (415, "dead", "permanent"), (422, "dead", "permanent"), (401, "retry", "config"), (403, "retry", "config"),
    (404, "retry", "config"), (410, "retry", "config"), (429, "retry", "throttle"), (500, "retry", "transport"),
    (502, "retry", "transport"), (503, "retry", "transport"),
])
def test_status_to_outcome(make_config, stubs, secret_file, code, kind, klass):
    s = stubs()
    s.respond({"status": code, "body": b"nope"})
    d = dest(make_config, url=s.url, secret=secret_file, allow_insecure=True)
    out = go(d)
    assert (out.kind, out.klass, out.status) == (kind, klass, code)
    assert SENTINEL not in out.reason + out.detail


def test_retry_after_on_throttle(make_config, stubs, secret_file):
    s = stubs()
    s.respond({"status": 429, "headers": {"Retry-After": "17"}})
    d = dest(make_config, url=s.url, secret=secret_file, allow_insecure=True)
    out = go(d)
    assert (out.klass, out.retry_after_s) == ("throttle", 17.0)


def test_2xx_body_is_not_interpreted(make_config, stubs, secret_file):
    s = stubs()
    s.respond({"status": 200, "body": b'{"status":"ignored"}'})
    d = dest(make_config, url=s.url, secret=secret_file, allow_insecure=True)
    out = go(d)
    assert out.kind == "ok" and out.notice is None


def test_connection_refused_is_transport(make_config, secret_file):
    s = Stub()
    url = s.url
    s.close()
    out = go(dest(make_config, url=url, secret=secret_file, allow_insecure=True))
    assert (out.klass, out.status) == ("transport", None)


# ---- secrets -------------------------------------------------------------------------------------
def test_secret_is_read_fresh_every_attempt(make_config, stubs, tmp_path):
    s = stubs()
    ref = write(tmp_path / "s", "first")
    d = dest(make_config, url=s.url, secret=ref, allow_insecure=True)
    go(d)
    (tmp_path / "s").write_text("second")
    go(d)
    a, b = s.requests
    assert verify_standard_webhooks(a["headers"], a["body"], "first", now=time.time())
    assert verify_standard_webhooks(b["headers"], b["body"], "second", now=time.time())
    assert not verify_standard_webhooks(b["headers"], b["body"], "first", now=time.time())


def test_secret_previous_adds_a_second_signature(make_config, stubs, tmp_path):
    s = stubs()
    d = dest(make_config, url=s.url, allow_insecure=True, secret=write(tmp_path / "new", "new-secret"),
             secret_previous=write(tmp_path / "old", "old-secret"))
    go(d)
    r = s.requests[0]
    assert len(r["headers"]["webhook-signature"].split(" ")) == 2
    for sec in ("new-secret", "old-secret"):
        assert verify_standard_webhooks(r["headers"], r["body"], sec, now=time.time())


def test_whsec_secret_uses_the_decoded_key(make_config, stubs, tmp_path):
    s = stubs()
    sec = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
    d = dest(make_config, url=s.url, allow_insecure=True, secret=write(tmp_path / "w", sec))
    go(d)
    r = s.requests[0]
    assert verify_standard_webhooks(r["headers"], r["body"], sec, now=time.time())


@pytest.mark.parametrize("make_ref", [
    lambda tmp: {"file": str(tmp / "missing")},
    lambda tmp: {"env": "HOOKSPOOL_TEST_UNSET_VAR"},
    lambda tmp: {"env_file": str(tmp / "nope.env"), "key": "K"},
    lambda tmp: "a literal secret",
    lambda tmp: None,
])
def test_unresolvable_secret_is_config_and_nothing_is_sent(make_config, stubs, tmp_path, make_ref):
    s = stubs()
    spec = {"url": s.url, "allow_insecure": True}
    ref = make_ref(tmp_path)
    if ref is not None:
        spec["secret"] = ref
    d = dest(make_config, **spec)
    out = go(d)
    assert (out.kind, out.klass) == ("retry", "config") and s.requests == []
    assert "a literal secret" not in out.reason + out.detail
    assert d.check()


def test_unusable_whsec_secret_is_config(make_config, stubs, tmp_path):
    s = stubs()
    d = dest(make_config, url=s.url, allow_insecure=True, secret=write(tmp_path / "w", "whsec_!!!"))
    out = go(d)
    assert out.klass == "config" and s.requests == [] and "whsec_!!!" not in out.reason + out.detail


# ---- headers --------------------------------------------------------------------------------------
def test_static_headers_are_sent_but_cannot_override_ours(make_config, stubs, secret_file):
    s = stubs()
    d = dest(make_config, url=s.url, secret=secret_file, allow_insecure=True,
             headers={"X-Env": "prod", "webhook-id": "forged", "Content-Type": "text/plain",
                      "x-hookspool-batch": "99", "X-Bad": "a\r\nInjected: 1"})
    go(d, delivery_id="hs_inst_7", ctx=make_ctx(batch_id=7, wall=True))
    h = s.requests[0]["headers"]
    assert h["X-Env"] == "prod" and h["webhook-id"] == "hs_inst_7"
    assert h["Content-Type"] == "application/json" and h["X-Hookspool-Batch"] == "7"
    assert "X-Bad" not in h and "Injected" not in h
    probs = " ".join(d.check())
    assert "webhook-id" in probs and "Content-Type" in probs and "line break" in probs


def test_per_batch_id_is_identical_on_every_attempt(make_config, stubs, secret_file):
    s = stubs()
    d = dest(make_config, url=s.url, secret=secret_file, allow_insecure=True)
    go(d, delivery_id="hs_inst_7", ctx=make_ctx(attempt=1, now=1_800_000_000.0))
    go(d, delivery_id="hs_inst_7", ctx=make_ctx(attempt=2, now=1_800_000_020.0))
    a, b = s.requests
    assert a["headers"]["webhook-id"] == b["headers"]["webhook-id"] == "hs_inst_7"
    assert a["headers"]["webhook-timestamp"] != b["headers"]["webhook-timestamp"]


# ---- bounds ----------------------------------------------------------------------------------------
def test_stalled_receiver_returns_at_the_timeout(make_config, stubs, secret_file):
    s = stubs()
    hold = threading.Event()
    s.respond({"status": 200, "hold": hold})
    d = dest(make_config, url=s.url, secret=secret_file, allow_insecure=True, timeout_s=0.4)
    t0 = time.monotonic()
    out = go(d)
    hold.set()
    assert (out.klass, out.reason) == ("transport", "timeout") and time.monotonic() - t0 < 2.0


def test_stalled_receiver_returns_at_the_worker_deadline(make_config, stubs, secret_file):
    s = stubs()
    hold = threading.Event()
    s.respond({"status": 200, "hold": hold})
    d = dest(make_config, url=s.url, secret=secret_file, allow_insecure=True, timeout_s=60)
    t0 = time.monotonic()
    out = go(d, ctx=make_ctx(wall=True, ttl=0.4))
    hold.set()
    assert (out.klass, out.reason) == ("transport", "deadline") and time.monotonic() - t0 < 2.0


def test_dripping_receiver_cannot_outlive_the_deadline(make_config, stubs, secret_file):
    s = stubs()
    s.respond({"status": 200, "drip": 0.1})
    d = dest(make_config, url=s.url, secret=secret_file, allow_insecure=True, timeout_s=60)
    t0 = time.monotonic()
    out = go(d, ctx=make_ctx(wall=True, ttl=0.5))
    assert out.klass == "transport" and time.monotonic() - t0 < 2.0


def test_hung_connect_returns_at_the_deadline(make_config, secret_file, monkeypatch):
    import http.client
    release = threading.Event()

    def hang(self):
        release.wait(30)
        raise OSError("released")
    monkeypatch.setattr(http.client.HTTPConnection, "connect", hang)
    d = dest(make_config, url="http://192.0.2.1:9/h", secret=secret_file, allow_insecure=True, timeout_s=60)
    t0 = time.monotonic()
    out = go(d, ctx=make_ctx(wall=True, ttl=0.4))
    release.set()
    assert (out.klass, out.reason) == ("transport", "deadline") and time.monotonic() - t0 < 2.0


# ---- check --------------------------------------------------------------------------------------
def test_check_flags_a_bad_url_and_never_prints_secrets(make_config, tmp_path):
    d = dest(make_config, url="ftp://x/y", secret=write(tmp_path / "s", "do-not-print-me"))
    probs = d.check()
    assert any("url" in p for p in probs) and "do-not-print-me" not in " ".join(probs)


# ---- through the dispatcher ----------------------------------------------------------------------
def test_dispatcher_round_trip_keeps_the_batch_id_across_attempts(rig, stubs, clock, tmp_path, secret_file):
    s = stubs()
    s.respond({"status": 500}, {"status": 200})
    r = rig({"destinations": {"audit": {"type": "http", "url": s.url + "/h", "allow_insecure": True,
                                        "secret": secret_file}}}, destination_factory=build)
    a = r.enqueue(dest="audit", n=1)
    r.d.drain_once()
    clock.advance(30)
    r.d.drain_once()
    assert r.batch(a.batch_id)["state"] == "delivered"
    ids = [q["headers"]["webhook-id"] for q in s.requests]
    assert ids == [f"hs_{r.store.instance_id()}_{a.batch_id}"] * 2
    assert all(verify_standard_webhooks(q["headers"], q["body"], "audit-secret-1", now=clock())
               for q in s.requests[1:])


def test_dispatcher_refuses_a_document_over_max_body_bytes(rig, stubs, secret_file):
    s = stubs()
    r = rig({"destinations": {"audit": {"type": "http", "url": s.url, "allow_insecure": True,
                                        "secret": secret_file, "max_body_bytes": 200}}},
            destination_factory=build)
    a = r.enqueue(dest="audit")
    r.d.drain_once()
    b = r.batch(a.batch_id)
    assert b["state"] == "dead" and s.requests == [] and "max_body_bytes" in (b["last_error"] or "")
