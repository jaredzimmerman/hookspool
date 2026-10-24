"""The network module, hookspool/net.py (Http, OAuth, secrets) and the verify.KeyCache key cache, against a local stub
HTTP server on 127.0.0.1. Test values only."""
from __future__ import annotations

import json
import os
import email.utils
import socket
import ssl
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest

from conftest import SENTINEL
from hookspool import net
from hookspool import verify as V
Http, Permanent, Retryable, Unauthorized = net.Http, net.Permanent, net.Retryable, net.Unauthorized

SECRET_MARK = "SECRET-SENTINEL-c41d"     # every test credential value carries this


# ---- the stub ---------------------------------------------------------------------------------
class Stub:
    """``routes[path]`` is a list of response specs popped per request (the last one sticks):
    ``{"status", "body" (bytes or obj), "headers", "delay", "drip"}``."""

    def __init__(self):
        self.routes: dict[str, list[dict]] = {}
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        stub = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _handle(self):
                n = int(self.headers.get("Content-Length", 0) or 0)
                body = self.rfile.read(n) if n else b""
                u = urlsplit(self.path)
                with stub.lock:
                    stub.requests.append({"method": self.command, "path": u.path,
                                          "query": {k: v[0] for k, v in parse_qs(u.query).items()},
                                          "headers": dict(self.headers.items()), "body": body})
                    specs = stub.routes.get(u.path) or [{"status": 404, "body": {"error": "not_found"}}]
                    spec = specs.pop(0) if len(specs) > 1 else specs[0]
                if spec.get("delay"):
                    time.sleep(spec["delay"])
                data = spec.get("body", {})
                if not isinstance(data, bytes):
                    data = json.dumps(data).encode()
                try:
                    self.send_response(spec.get("status", 200))
                    for k, v in (spec.get("headers") or {}).items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    if spec.get("drip"):
                        for i in range(len(data)):
                            self.wfile.write(data[i:i + 1])
                            self.wfile.flush()
                            time.sleep(spec["drip"])
                    else:
                        self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = do_POST = do_PUT = do_DELETE = do_CONNECT = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def on(self, path: str, *specs: dict) -> None:
        with self.lock:
            self.routes[path] = list(specs)

    def hits(self, path: str) -> list[dict]:
        with self.lock:
            return [r for r in self.requests if r["path"] == path]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.close()


def _no_leak(err: BaseException, *needles: str) -> None:
    text = "".join(traceback.format_exception(err)) + repr(err) + repr(err.args)
    for n in needles:
        assert n not in text, f"{n!r} leaked into {err!r}"
    assert err.__cause__ is None and err.__context__ is None


# ---- Http: success ------------------------------------------------------------------------------------
def test_get_json_with_params_and_headers(stub):
    stub.on("/v1/items", {"body": {"items": [1, 2]}})
    got = Http().get(stub.base + "/v1/items", params={"q": "a b", "n": 3}, bearer="tok-1", headers={"X-A": "1"})
    assert got == {"items": [1, 2]}
    r = stub.hits("/v1/items")[0]
    assert r["query"] == {"q": "a b", "n": "3"}
    assert r["headers"]["Authorization"] == "Bearer tok-1"
    assert r["headers"]["Accept"] == "application/json" and r["headers"]["X-A"] == "1"
    assert r["headers"]["User-Agent"].startswith("hookspool/")


def test_post_json_form_and_empty_body(stub):
    stub.on("/p", {"body": b""})
    assert Http().post(stub.base + "/p", json={"a": 1}) == {}
    assert Http().post(stub.base + "/p", form={"x": "1 2"}) == {}
    a, b = stub.hits("/p")
    assert json.loads(a["body"]) == {"a": 1} and a["headers"]["Content-Type"] == "application/json"
    assert b["body"] == b"x=1+2" and b["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    with pytest.raises(Permanent):
        Http().post(stub.base + "/p", json={}, form={})


# ---- Http: failure mapping -------------------------------------------------------------------------
@pytest.mark.parametrize("status", [500, 502, 503, 504, 429, 408, 425])
def test_retryable_status(stub, status):
    stub.on("/r", {"status": status, "body": {"error": "backend_error"}, "headers": {"Retry-After": "7"}})
    with pytest.raises(Retryable) as ei:
        Http().get(stub.base + "/r", label="items.list")
    e = ei.value
    assert e.status == status and e.retry_after_s == 7.0 and e.code == "backend_error"
    assert str(e) == f"GET 127.0.0.1 items.list: HTTP {status} backend_error"


@pytest.mark.parametrize("status", [400, 403, 404, 409, 410, 422])
def test_other_4xx_is_permanent(stub, status):
    stub.on("/p", {"status": status, "body": {"error": {"status": "PERMISSION_DENIED", "message": SENTINEL}}})
    with pytest.raises(Permanent) as ei:
        Http().get(stub.base + "/p")
    assert not isinstance(ei.value, (Retryable, Unauthorized))
    assert ei.value.status == status and ei.value.code == "PERMISSION_DENIED"
    _no_leak(ei.value, SENTINEL)


def test_401_is_unauthorized_and_permanent(stub):
    stub.on("/u", {"status": 401})
    with pytest.raises(Unauthorized) as ei:
        Http().get(stub.base + "/u")
    assert isinstance(ei.value, Permanent) and ei.value.status == 401


def test_redirect_is_not_followed(stub):
    stub.on("/moved", {"status": 302, "headers": {"Location": stub.base + "/elsewhere"}})
    stub.on("/elsewhere", {"body": {"ok": True}})
    with pytest.raises(Permanent) as ei:
        Http().get(stub.base + "/moved", bearer="tok-redirect")
    assert ei.value.status == 302
    assert stub.hits("/elsewhere") == []


def test_timeout_is_retryable(stub):
    stub.on("/slow", {"delay": 1.5})
    t0 = time.monotonic()
    with pytest.raises(Retryable) as ei:
        Http(timeout_s=0.3).get(stub.base + "/slow")
    assert time.monotonic() - t0 < 1.2
    assert "timed out" in str(ei.value)


def test_dripping_response_is_bounded_by_the_total_timeout(stub):
    stub.on("/drip", {"body": {"pad": "x" * 200}, "drip": 0.02})       # ~4 s to send it all
    t0 = time.monotonic()
    with pytest.raises(Retryable):
        Http(timeout_s=0.5).get(stub.base + "/drip")
    assert time.monotonic() - t0 < 1.5


def test_connection_refused_is_retryable():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(Retryable):
        Http(timeout_s=2).get(f"http://127.0.0.1:{port}/x")


def test_not_json_is_retryable_and_oversize_is_permanent(stub):
    stub.on("/html", {"body": b"<html>" + SENTINEL.encode() + b"</html>"})
    with pytest.raises(Retryable) as ei:
        Http().get(stub.base + "/html")
    _no_leak(ei.value, SENTINEL)
    stub.on("/big", {"body": {"pad": "y" * 5000}})
    with pytest.raises(Permanent) as ei:
        Http(max_response_bytes=1000).get(stub.base + "/big")
    assert "larger than 1000 bytes" in str(ei.value)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "http://192.0.2.1/x",
                                 "https:///nohost", "gopher://127.0.0.1/"])
def test_refused_urls(url):
    with pytest.raises(Permanent):
        Http(timeout_s=0.5).get(url)


def test_insecure_http_opt_in_reaches_the_network(monkeypatch):
    # allow_insecure lifts the loopback-only rule. The test must not connect to a non-loopback address:
    # on macOS a process that has done so can no longer fork (Network.framework's fork handler crashes
    # the child), which broke every later child-process test of the suite. The opener is faked instead.
    seen = []

    def fake_open(self, req, timeout=None):
        seen.append(req.full_url)
        raise urllib.error.URLError(ConnectionRefusedError())

    monkeypatch.setattr(type(Http()._opener), "open", fake_open)
    with pytest.raises(Retryable):
        Http(timeout_s=0.3, allow_insecure=True).get("http://192.0.2.1/x")
    assert seen == ["http://192.0.2.1/x"]
    seen.clear()
    with pytest.raises(Permanent):                           # without the opt-in it never gets that far
        Http(timeout_s=0.3).get("http://192.0.2.1/x")
    assert seen == []


def test_errors_never_carry_path_query_body_or_headers(stub):
    path = f"/bot{SENTINEL}/send"
    stub.on(path, {"status": 500, "body": {"error": SENTINEL, "detail": SENTINEL}})
    with pytest.raises(Retryable) as ei:
        Http().post(stub.base + path + f"?token={SENTINEL}", json={"x": SENTINEL}, params={"k": SENTINEL},
                    bearer=SENTINEL, headers={"X-Key": SENTINEL})
    _no_leak(ei.value, SENTINEL)
    assert ei.value.code == ""                               # the body's "error" was not a safe code
    stub.on("/t", {"delay": 1})
    with pytest.raises(Retryable) as ei:
        Http(timeout_s=0.2).get(stub.base + f"/t?token={SENTINEL}", label=SENTINEL + "!")
    _no_leak(ei.value, SENTINEL)


def test_header_injection_is_refused(stub):
    with pytest.raises(Permanent) as ei:
        Http().get(stub.base + "/x", headers={"X-A": "a\r\nX-B: " + SENTINEL})
    _no_leak(ei.value, SENTINEL)


# ---- secrets --------------------------------------------------------------------------------------------
def test_secret_access_through_config_refs(tmp_path):
    f = tmp_path / "s"
    f.write_text("value-" + SECRET_MARK + "\n")
    sec = net.SecretAccess(net.resolver_from_refs({"a": {"file": str(f)}, "gone": {"file": str(tmp_path / "x")}}))
    assert sec("a") == "value-" + SECRET_MARK
    with pytest.raises(Permanent):
        sec("not-declared")
    with pytest.raises(Retryable) as ei:
        sec("gone")
    _no_leak(ei.value, SECRET_MARK)
    f.write_text("rotated-" + SECRET_MARK)                  # read at call time: rotation is picked up
    assert sec("a") == "rotated-" + SECRET_MARK


# ---- OAuth -------------------------------------------------------------------------------------------------
class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _cred(tmp_path, stub, **over) -> str:
    c = {"client_id": "cid-" + SECRET_MARK, "client_secret": "csec-" + SECRET_MARK,
         "refresh_token": "rt-0-" + SECRET_MARK, "token_uri": stub.base + "/token"}
    c.update(over)
    p = tmp_path / "cred.json"
    p.write_text(json.dumps(c))
    os.chmod(p, 0o600)
    return str(p)


def _token(n: int, *, expires_in=3600, refresh_token=None, status=200):
    body = {"access_token": f"at-{n}-{SECRET_MARK}", "expires_in": expires_in, "token_type": "Bearer"}
    if refresh_token:
        body["refresh_token"] = refresh_token
    return {"status": status, "body": body}


def _extras(tmp_path, cred_path, state, clock, lines, persist=None):
    return net.make_context_extras(state, net.resolver_from_refs({"cred_owner": {"file": cred_path}}),
                                   log=lines.append, now=clock, persist=persist)


def _form(req) -> dict:
    return {k: v[0] for k, v in parse_qs(req["body"].decode()).items()}


def test_make_context_extras_shape(tmp_path, stub):
    ex = net.make_context_extras({}, lambda name: "v")
    assert set(ex) == {"http", "oauth", "secret", "pace"}
    assert isinstance(ex["http"], Http) and ex["secret"]("x") == "v"
    assert isinstance(ex["oauth"]("cred"), net.OAuthClient)


def test_access_token_is_cached_until_expiry(tmp_path, stub):
    cred = _cred(tmp_path, stub)
    stub.on("/token", _token(1, expires_in=600), _token(2))
    stub.on("/api", {"body": {"ok": True}})
    state, clock, lines = {}, Clock(), []
    ex = _extras(tmp_path, cred, state, clock, lines)
    c = ex["oauth"]("cred_owner")
    assert c.get(stub.base + "/api") == {"ok": True}
    assert c.get(stub.base + "/api") == {"ok": True}
    assert len(stub.hits("/token")) == 1
    form = _form(stub.hits("/token")[0])
    assert form == {"grant_type": "refresh_token", "client_id": "cid-" + SECRET_MARK,
                    "client_secret": "csec-" + SECRET_MARK, "refresh_token": "rt-0-" + SECRET_MARK}
    assert [r["headers"]["Authorization"] for r in stub.hits("/api")] == [f"Bearer at-1-{SECRET_MARK}"] * 2
    assert state["oauth"]["cred_owner"]["access_token"] == f"at-1-{SECRET_MARK}"
    assert ex["oauth"].changed
    clock.t += 600 - net.TOKEN_SKEW_S + 1                   # inside the skew before expiry: refresh
    c.get(stub.base + "/api")
    assert len(stub.hits("/token")) == 2
    assert stub.hits("/api")[-1]["headers"]["Authorization"] == f"Bearer at-2-{SECRET_MARK}"
    # a new runtime with the saved state reuses the token (no refresh)
    ex2 = _extras(tmp_path, cred, json.loads(json.dumps(state)), clock, [])
    ex2["oauth"]("cred_owner").get(stub.base + "/api")
    assert len(stub.hits("/token")) == 2


def test_one_401_triggers_one_refresh(tmp_path, stub):
    cred = _cred(tmp_path, stub)
    stub.on("/token", _token(1), _token(2), _token(3))
    stub.on("/api", {"status": 401}, {"body": {"ok": 1}})
    c = _extras(tmp_path, cred, {}, Clock(), [])["oauth"]("cred_owner")
    assert c.get(stub.base + "/api") == {"ok": 1}
    assert len(stub.hits("/token")) == 2
    assert [r["headers"]["Authorization"] for r in stub.hits("/api")] == [
        f"Bearer at-1-{SECRET_MARK}", f"Bearer at-2-{SECRET_MARK}"]
    # a second 401 after the refresh gives up: Permanent, and still only one more refresh
    stub.on("/api", {"status": 401})
    with pytest.raises(Unauthorized) as ei:
        c.get(stub.base + "/api")
    assert len(stub.hits("/token")) == 3
    _no_leak(ei.value, SECRET_MARK)


def test_403_does_not_refresh(tmp_path, stub):
    cred = _cred(tmp_path, stub)
    stub.on("/token", _token(1))
    stub.on("/api", {"status": 403})
    c = _extras(tmp_path, cred, {}, Clock(), [])["oauth"]("cred_owner")
    with pytest.raises(Permanent):
        c.get(stub.base + "/api")
    assert len(stub.hits("/token")) == 1


def test_refresh_token_rotation_kept_in_state_never_in_the_file(tmp_path, stub):
    cred = _cred(tmp_path, stub)
    before, mtime = open(cred, "rb").read(), os.stat(cred).st_mtime_ns
    stub.on("/token", _token(1, refresh_token="rt-1-" + SECRET_MARK), _token(2, refresh_token="rt-2-" + SECRET_MARK),
            _token(3))
    stub.on("/api", {"body": {}})
    state, clock, lines = {}, Clock(), []
    ex = _extras(tmp_path, cred, state, clock, lines)
    ex["oauth"]("cred_owner").get(stub.base + "/api")
    assert state["oauth"]["cred_owner"]["refresh_token"] == "rt-1-" + SECRET_MARK
    clock.t += 3600
    ex["oauth"]("cred_owner").get(stub.base + "/api")
    clock.t += 3600
    ex["oauth"]("cred_owner").get(stub.base + "/api")
    used = [_form(r)["refresh_token"] for r in stub.hits("/token")]
    assert used == ["rt-0-" + SECRET_MARK, "rt-1-" + SECRET_MARK, "rt-2-" + SECRET_MARK]
    assert state["oauth"]["cred_owner"]["refresh_token"] == "rt-2-" + SECRET_MARK   # no new one: kept
    assert open(cred, "rb").read() == before and os.stat(cred).st_mtime_ns == mtime
    assert sorted(os.listdir(tmp_path)) == ["cred.json"]
    assert any("rotated refresh token kept in state" in ln for ln in lines)
    for ln in lines:
        assert SECRET_MARK not in ln


def test_reauthorised_credential_drops_the_cached_entry(tmp_path, stub):
    cred = _cred(tmp_path, stub)
    stub.on("/token", _token(1, refresh_token="rt-1-" + SECRET_MARK), _token(2))
    stub.on("/api", {"body": {}})
    state, clock = {}, Clock()
    ex = _extras(tmp_path, cred, state, clock, [])
    ex["oauth"]("cred_owner").get(stub.base + "/api")
    _cred(tmp_path, stub, refresh_token="rt-new-" + SECRET_MARK)          # someone re-authorised
    ex["oauth"]("cred_owner").get(stub.base + "/api")
    assert _form(stub.hits("/token")[-1])["refresh_token"] == "rt-new-" + SECRET_MARK
    assert "refresh_token" not in state["oauth"]["cred_owner"]
    assert stub.hits("/api")[-1]["headers"]["Authorization"] == f"Bearer at-2-{SECRET_MARK}"


def test_refused_rotated_token_falls_back_to_the_credentials_own(tmp_path, stub):
    cred = _cred(tmp_path, stub)
    state = {"oauth": {}}
    ex = _extras(tmp_path, cred, state, Clock(), [])
    stub.on("/token", _token(0))
    ex["oauth"]("cred_owner").token()
    state["oauth"]["cred_owner"].update(refresh_token="rt-lost-" + SECRET_MARK, expires_at=0)
    stub.requests.clear()
    stub.on("/token", {"status": 400, "body": {"error": "invalid_grant"}}, _token(5))
    assert ex["oauth"]("cred_owner").token() == f"at-5-{SECRET_MARK}"
    assert [_form(r)["refresh_token"] for r in stub.hits("/token")] == ["rt-lost-" + SECRET_MARK,
                                                                       "rt-0-" + SECRET_MARK]
    assert "refresh_token" not in state["oauth"]["cred_owner"]


def test_refresh_failures(tmp_path, stub):
    cred = _cred(tmp_path, stub)
    lines = []
    c = _extras(tmp_path, cred, {}, Clock(), lines)["oauth"]("cred_owner")
    stub.on("/token", {"status": 400, "body": {"error": "invalid_grant", "error_description": SECRET_MARK}})
    with pytest.raises(Permanent) as ei:
        c.token()
    assert "invalid_grant" in str(ei.value) and str(ei.value).startswith("oauth cred_owner: token refresh refused")
    _no_leak(ei.value, SECRET_MARK)
    stub.on("/token", {"status": 401, "body": {"error": "invalid_client"}})
    with pytest.raises(Permanent) as ei:
        c.token()
    assert not isinstance(ei.value, Unauthorized)
    stub.on("/token", {"status": 503, "headers": {"Retry-After": "30"}})
    with pytest.raises(Retryable) as ei:
        c.token()
    assert ei.value.retry_after_s == 30.0
    stub.on("/token", {"body": {"token_type": "Bearer"}})
    with pytest.raises(Retryable):
        c.token()
    stub.on("/token", {"delay": 1.0})
    c2 = net.make_context_extras({}, net.resolver_from_refs({"cred_owner": {"file": cred}}),
                                 http=Http(timeout_s=0.2))["oauth"]("cred_owner")
    with pytest.raises(Retryable) as ei:
        c2.token()
    _no_leak(ei.value, SECRET_MARK)
    for ln in lines:
        assert SECRET_MARK not in ln


@pytest.mark.parametrize("content", ["not json " + SECRET_MARK, "[1]", json.dumps({"client_id": SECRET_MARK})])
def test_bad_credential_is_permanent_and_quiet(tmp_path, content):
    p = tmp_path / "cred.json"
    p.write_text(content)
    c = net.make_context_extras({}, net.resolver_from_refs({"cred_owner": {"file": str(p)}}))["oauth"]("cred_owner")
    with pytest.raises(Permanent) as ei:
        c.token()
    _no_leak(ei.value, SECRET_MARK)


def test_insecure_token_uri_is_refused(tmp_path, stub):
    cred = _cred(tmp_path, stub, token_uri="http://192.0.2.1/token")
    c = _extras(tmp_path, cred, {}, Clock(), [])["oauth"]("cred_owner")
    with pytest.raises(Permanent) as ei:
        c.token()
    _no_leak(ei.value, SECRET_MARK)


def test_nothing_secret_reaches_logs_or_reprs(tmp_path, stub, caplog):
    cred = _cred(tmp_path, stub)
    stub.on("/token", _token(1, refresh_token="rt-1-" + SECRET_MARK))
    stub.on("/api", {"status": 401}, {"status": 500, "body": {"error": SECRET_MARK}})
    state, lines = {}, []
    ex = _extras(tmp_path, cred, state, Clock(), lines)
    with pytest.raises(Retryable) as ei:
        ex["oauth"]("cred_owner").get(stub.base + "/api", params={"q": SECRET_MARK})
    _no_leak(ei.value, SECRET_MARK)
    blob = "\n".join(lines) + caplog.text + repr(ex) + repr(ex["oauth"]("cred_owner")) + repr(ex["secret"])
    assert SECRET_MARK not in blob and lines


# ---- verify.KeyCache (JWKS) -------------------------------------------------------------------------------
def _jwk(kid):
    return {"kty": "EC", "crv": "P-256", "kid": kid, "x": "x", "y": "y"}


class Fetch:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, kid):
        self.calls.append(kid)
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, BaseException):
            raise a
        return a


def test_keycache_ttl_and_refetch_after_expiry():
    f = Fetch({"keys": [_jwk("k1")]})
    kc = V.KeyCache(f, ttl_s=100, cooldown_s=10)
    assert kc.lookup("k1", 0)["kid"] == "k1"
    assert kc.lookup("k1", 99)["kid"] == "k1"
    assert len(f.calls) == 1
    assert kc.lookup("k1", 101)["kid"] == "k1"            # past TTL: refetched
    assert len(f.calls) == 2


def test_keycache_unknown_kid_refetches_the_set_once_per_cooldown():
    f = Fetch({"keys": [_jwk("k1")]}, {"keys": [_jwk("k1")]}, {"keys": [_jwk("k1"), _jwk("k2")]})
    kc = V.KeyCache(f, cooldown_s=60, negative_ttl_s=600)
    assert kc.lookup("k1", 0) is not None
    assert kc.lookup("k2", 30) is None                    # unknown, inside the cooldown: no fetch
    assert len(f.calls) == 1
    assert kc.lookup("k2", 61) is None                    # fetched; not in the set: negative-cached
    assert len(f.calls) == 2
    assert kc.lookup("k2", 200) is None                   # negative cache: no fetch
    assert len(f.calls) == 2
    assert kc.lookup("k3", 200) is None                   # another new kid: refetched, not listed either
    assert len(f.calls) == 3
    assert kc.lookup("k2", 200)["kid"] == "k2"            # the third fetch listed k2: cached from the set


def test_keycache_serves_stale_key_while_refresh_fails_and_drops_withdrawn():
    f = Fetch({"keys": [_jwk("k1")]}, OSError("down"), {"keys": [_jwk("k9")]})
    errors = []
    kc = V.KeyCache(f, ttl_s=100, cooldown_s=10, on_error=errors.append)
    assert kc.lookup("k1", 0) is not None
    assert kc.lookup("k1", 150)["kid"] == "k1"           # refresh failed: stale key still served
    assert [e.__class__.__name__ for e in errors] == ["OSError"]
    assert kc.lookup("k1", 155)["kid"] == "k1"           # cooldown: stale, no fetch
    assert len(f.calls) == 2
    assert kc.lookup("k1", 170) is None                  # a good set without k1: revoked
    assert kc.lookup("k9", 171)["kid"] == "k9"


def test_keycache_single_key_mode_and_usable():
    f = Fetch(_jwk("a"), {"kid": "b", "expired_at": "2020"}, None)
    kc = V.KeyCache(f, cooldown_s=0, usable=lambda j: j.get("expired_at") is None)
    assert kc.lookup("a", 0)["kid"] == "a"
    assert kc.lookup("b", 1) is None
    assert kc.lookup("c", 2) is None
    assert f.calls == ["a", "b", "c"]
    assert kc.lookup("", 3) is None and kc.lookup(None, 3) is None


def test_keycache_bounded():
    f = Fetch(None)
    kc = V.KeyCache(f, cooldown_s=0, max_cached=8)
    for i in range(50):
        kc.lookup(f"r{i}", float(i))
    assert len(kc.unknown) <= 8


def test_jwks_fetcher_against_stub(stub):
    stub.on("/jwks", {"body": {"keys": [_jwk("k1"), {"no": "kid"}, "junk"]}}, {"status": 503})
    kc = V.KeyCache(V.jwks_fetcher(stub.base + "/jwks"), ttl_s=10, cooldown_s=1)
    assert kc.lookup("k1", 0)["kid"] == "k1"
    assert kc.lookup("k1", 20)["kid"] == "k1"            # 503 on refresh: stale key served
    assert len(stub.hits("/jwks")) == 2
    stub.on("/jwks", {"body": {"not": "a set"}})
    assert kc.lookup("zz", 40) is None


def test_keycache_lookup_takes_its_own_fetch_and_needs_one():
    f = Fetch(_jwk("a"))
    kc = V.KeyCache(cooldown_s=0)
    with pytest.raises(ValueError):
        kc.lookup("a", 0)
    assert kc.lookup("a", 0, fetch=f)["kid"] == "a"
    assert kc.lookup("a", 1)["kid"] == "a"               # cached: no fetch needed any more
    assert f.calls == ["a"]


def test_keycache_logs_a_failed_fetch_once_without_a_body(caplog):
    caplog.set_level("WARNING", logger="hookspool.verify")
    kc = V.KeyCache(cooldown_s=0, name="acme sandbox")
    kc.lookup("a", 0, fetch=Fetch(OSError("down " + SECRET_MARK)))
    kc.lookup("b", 1, fetch=Fetch(net.Permanent("POST acme.test: HTTP 400 BAD_KEY", status=400, code="BAD_KEY")))
    lines = [r.getMessage() for r in caplog.records]
    assert lines == ["acme sandbox key fetch failed: OSError",
                     "acme sandbox key fetch failed: Permanent http=400 error_code=BAD_KEY"]
    assert SECRET_MARK not in caplog.text


def test_keycache_does_not_log_the_status_or_code_of_a_caller_supplied_exception(caplog):
    """Only ``net.NetError`` carries a sanitised code; a free-text ``.code`` on any other exception stays out."""
    class Odd(Exception):
        status = 500
        code = "free text " + SECRET_MARK
    caplog.set_level("WARNING", logger="hookspool.verify")
    kc = V.KeyCache(cooldown_s=0, name="acme sandbox")
    kc.lookup("a", 0, fetch=Fetch(Odd("boom")))
    assert [r.getMessage() for r in caplog.records] == ["acme sandbox key fetch failed: Odd"]
    assert SECRET_MARK not in caplog.text


def test_shared_key_cache_is_one_per_name_and_resets_by_prefix():
    V.reset_shared_key_caches("unit-test ")
    a = V.shared_key_cache("unit-test one", ttl_s=5)
    assert V.shared_key_cache("unit-test one", ttl_s=99) is a and a.ttl_s == 5
    b = V.shared_key_cache("unit-test two")
    assert b is not a
    V.reset_shared_key_caches("unit-test one")
    assert V.shared_key_cache("unit-test one") is not a
    assert V.shared_key_cache("unit-test two") is b
    V.reset_shared_key_caches("unit-test ")


def test_key_fetcher_posts_a_body_and_maps_unknown_key_refusals(stub):
    jwk = _jwk("k1")
    stub.on("/key", {"body": {"key": jwk, "request_id": "r"}}, {"status": 400, "body": {"error_code": "NO_SUCH_KEY"}},
            {"status": 404, "body": {}}, {"status": 400, "body": {"error_code": "BAD_CREDENTIALS"}},
            {"status": 503}, {"body": {"nothing": 1}})

    def unknown(status, code):
        return status == 404 or (status == 400 and code == "NO_SUCH_KEY")
    fetch = V.key_fetcher(stub.base + "/key", method="POST", body=lambda kid: {"key_id": kid, "k": SECRET_MARK},
                          field="key", unknown=unknown)
    assert fetch("k1") == jwk
    assert json.loads(stub.hits("/key")[0]["body"]) == {"key_id": "k1", "k": SECRET_MARK}
    assert fetch("k2") is None                                   # 400 NO_SUCH_KEY: the sender does not know it
    assert fetch("k3") is None                                   # 404
    with pytest.raises(Permanent) as e:
        fetch("k4")                                              # any other refusal is an error, not "unknown"
    assert (e.value.status, e.value.code) == (400, "BAD_CREDENTIALS") and SECRET_MARK not in str(e.value)
    with pytest.raises(Retryable):
        fetch("k5")
    assert fetch("k6") is None                                   # a 200 without the key member


def test_key_fetcher_without_a_field_wants_a_jwks_set(stub):
    stub.on("/set", {"body": {"keys": [_jwk("k1")]}}, {"body": {"key": _jwk("k1")}})
    fetch = V.key_fetcher(stub.base + "/set")
    assert fetch("k1")["keys"][0]["kid"] == "k1"
    with pytest.raises(ValueError):
        fetch("k1")


# ---- rotated refresh token is persisted at once (a kill before the batch ends must not lose it) -------------
def test_persist_is_called_right_after_a_rotation_and_a_restart_uses_the_new_token(tmp_path, stub):
    cred = _cred(tmp_path, stub)
    stub.on("/token", _token(1, refresh_token="rt-1-" + SECRET_MARK), _token(2))
    stub.on("/api", {"body": {}})
    state, clock, lines = {}, Clock(), []
    disk = tmp_path / "runtime.json"
    calls = []

    def persist():                                             # the runtime's atomic writer, in miniature
        calls.append(json.loads(json.dumps(state)))
        disk.write_text(json.dumps(state))

    ex = _extras(tmp_path, cred, state, clock, lines, persist=persist)
    ex["oauth"]("cred_owner").get(stub.base + "/api")
    assert len(calls) == 1                                     # once, at the rotation
    assert calls[0]["oauth"]["cred_owner"]["refresh_token"] == "rt-1-" + SECRET_MARK
    assert calls[0]["oauth"]["cred_owner"]["access_token"] == f"at-1-{SECRET_MARK}"
    # the worker is killed here: nothing after the rotation ran. A new runtime starts from the file alone.
    state2 = json.loads(disk.read_text())
    clock.t += 3600
    ex2 = _extras(tmp_path, cred, state2, clock, [], persist=persist)
    ex2["oauth"]("cred_owner").get(stub.base + "/api")
    assert [_form(r)["refresh_token"] for r in stub.hits("/token")] == ["rt-0-" + SECRET_MARK,
                                                                           "rt-1-" + SECRET_MARK]
    assert len(calls) == 1                                     # the second refresh rotated nothing


def test_persist_is_not_called_without_a_rotation_and_its_failure_is_survivable(tmp_path, stub):
    cred = _cred(tmp_path, stub)
    stub.on("/token", _token(1), _token(2, refresh_token="rt-1-" + SECRET_MARK))
    stub.on("/api", {"body": {"ok": 1}})
    state, clock, lines, calls = {}, Clock(), [], []

    def persist():
        calls.append(1)
        raise OSError(f"disk full {SECRET_MARK}")

    ex = _extras(tmp_path, cred, state, clock, lines, persist=persist)
    assert ex["oauth"]("cred_owner").get(stub.base + "/api") == {"ok": 1}
    assert calls == []
    clock.t += 3600
    assert ex["oauth"]("cred_owner").get(stub.base + "/api") == {"ok": 1}     # the failing write did not fail it
    assert calls == [1]
    assert state["oauth"]["cred_owner"]["refresh_token"] == "rt-1-" + SECRET_MARK
    assert ex["oauth"].changed                                  # the runtime's final save still has work to do
    assert any("persisting the rotated refresh token failed (OSError)" in ln for ln in lines)
    for ln in lines:
        assert SECRET_MARK not in ln


# ---- proxies come from the environment (a deployment sets them through the processor env table) ---------
def test_http_proxy_from_the_environment_is_honoured_and_no_proxy_suppresses_it(monkeypatch):
    proxy, target = Stub(), Stub()
    try:
        proxy.on("/x", {"body": {"via": "proxy"}})
        target.on("/x", {"body": {"via": "direct"}})
        monkeypatch.setenv("HTTP_PROXY", proxy.base)
        assert Http().get(target.base + "/x") == {"via": "proxy"}       # a request to another loopback port
        assert len(proxy.hits("/x")) == 1 and target.hits("/x") == []
        assert proxy.hits("/x")[0]["headers"]["Host"] == target.base.split("//")[1]
        monkeypatch.setenv("NO_PROXY", "127.0.0.1")
        assert Http().get(target.base + "/x") == {"via": "direct"}
        assert len(proxy.hits("/x")) == 1 and len(target.hits("/x")) == 1
        monkeypatch.delenv("HTTP_PROXY")
        monkeypatch.delenv("NO_PROXY")
        assert Http().get(target.base + "/x") == {"via": "direct"}      # no proxy configured: direct
        assert len(proxy.hits("/x")) == 1 and len(target.hits("/x")) == 2
    finally:
        proxy.close()
        target.close()


def test_https_proxy_from_the_environment_is_used_for_https_and_no_proxy_suppresses_it(monkeypatch):
    proxy = Stub()
    try:
        monkeypatch.setenv("HTTPS_PROXY", proxy.base)
        with pytest.raises(Retryable):                                  # the stub refuses the tunnel
            Http(timeout_s=2).get("https://127.0.0.1:9/x")
        assert [r["method"] for r in proxy.requests] == ["CONNECT"]
        monkeypatch.setenv("NO_PROXY", "127.0.0.1")
        with pytest.raises(Retryable):                                  # direct: connection refused
            Http(timeout_s=2).get("https://127.0.0.1:9/x")
        assert len(proxy.requests) == 1
    finally:
        proxy.close()


# ---- small edges ------------------------------------------------------------------------------------------
def test_retry_after_as_an_http_date(stub):
    when = email.utils.formatdate(time.time() + 30, usegmt=True)
    stub.on("/d", {"status": 503, "headers": {"Retry-After": when}})
    with pytest.raises(Retryable) as ei:
        Http().get(stub.base + "/d")
    assert 20 <= ei.value.retry_after_s <= 31
    stub.on("/p", {"status": 503, "headers": {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}})
    with pytest.raises(Retryable) as ei:
        Http().get(stub.base + "/p")
    assert ei.value.retry_after_s == 0.0                                # a date in the past: retry now
    stub.on("/j", {"status": 503, "headers": {"Retry-After": "soon"}})
    with pytest.raises(Retryable) as ei:
        Http().get(stub.base + "/j")
    assert ei.value.retry_after_s is None


@pytest.mark.parametrize("junk", ["a string", ["a", "list"], 7, {"cred_owner": "not a dict"},
                                  {"cred_owner": ["x"]}, {"cred_owner": None}])
def test_corrupt_oauth_state_is_replaced_not_a_crash(tmp_path, stub, junk):
    cred = _cred(tmp_path, stub)
    stub.on("/token", _token(1))
    stub.on("/api", {"body": {}})
    state = {"oauth": junk}
    ex = _extras(tmp_path, cred, state, Clock(), [])
    ex["oauth"]("cred_owner").get(stub.base + "/api")
    assert state["oauth"]["cred_owner"]["access_token"] == f"at-1-{SECRET_MARK}"


def test_the_callers_own_bearer_is_stripped_on_an_oauth_request(tmp_path, stub):
    cred = _cred(tmp_path, stub)
    stub.on("/token", _token(1))
    stub.on("/api", {"body": {}})
    ex = _extras(tmp_path, cred, {}, Clock(), [])
    ex["oauth"]("cred_owner").get(stub.base + "/api", bearer="caller-supplied-token")
    assert stub.hits("/api")[0]["headers"]["Authorization"] == f"Bearer at-1-{SECRET_MARK}"


def test_a_tls_certificate_failure_is_permanent(monkeypatch):
    def fake_open(self, req, timeout=None):
        raise urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))

    monkeypatch.setattr(type(Http()._opener), "open", fake_open)
    with pytest.raises(Permanent) as ei:
        Http().get("https://api.example.test/x?token=" + SENTINEL, label="probe")
    assert "certificate" in str(ei.value) and SENTINEL not in str(ei.value)
    assert ei.value.__cause__ is None and ei.value.__context__ is None


def test_a_dripping_error_body_is_bounded_by_the_request_deadline(stub):
    stub.on("/slow", {"status": 500, "body": b"x" * 4000, "drip": 0.2})
    t0 = time.monotonic()
    with pytest.raises(Retryable) as ei:
        Http(timeout_s=0.6).get(stub.base + "/slow")
    assert time.monotonic() - t0 < 3
    assert ei.value.status == 500


def test_secret_access_forwards_a_secret_error_text_but_never_a_value():
    from hookspool.config import SecretError

    def res(name):
        raise SecretError("env var NAME is not set")

    with pytest.raises(Retryable) as ei:
        net.SecretAccess(res)("tok")
    assert "NAME" in str(ei.value)                                      # names the variable, as documented
    # an undeclared name through a mapping that raises SecretError (not KeyError) is Retryable, documented
    with pytest.raises(Permanent):
        net.SecretAccess(lambda n: (_ for _ in ()).throw(KeyError(n)))("tok")
