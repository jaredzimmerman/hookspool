"""Intake (contract C4; ARCHITECTURE §9, §10, §11, §14.1): the shared source kit on the
example plugins, plus the order of operations, quarantine, limits, /healthz, /metrics,
logs, the socket layer, captures and ``hookspool source test``."""
from __future__ import annotations

import http.client
import io
import json
import logging
import os
import threading
import types

import pytest

import kits
import sourcekit
from hookspool import config as hs_config
from hookspool import intake as I
from hookspool import logs as hs_logs
from hookspool import plugin as P
from hookspool.__main__ import main
from hookspool.store import SpoolError
from sourcekit import SENTINEL, Env, request

T0 = 1_800_000_000.0


# ---- the kit matrix on every example plugin -------------------------------------------------------------------
@pytest.mark.parametrize("kit", list(kits.ALL.values()), ids=list(kits.ALL))
@pytest.mark.parametrize("case", sourcekit.CASES, ids=sourcekit.CASE_IDS)
def test_kit_matrix(case, kit, tmp_path, clock):
    sourcekit.run(case, kit, tmp_path, clock)


CAPTURE_FILES = [(src, p) for src in kits.ALL for p in sourcekit.captures(src)]


@pytest.mark.parametrize("src,path", CAPTURE_FILES, ids=[f"{s}/{os.path.basename(p)}" for s, p in CAPTURE_FILES])
def test_captures(src, path, tmp_path, clock):
    out = sourcekit.run_capture(path, kits.ALL[src], tmp_path, clock)
    assert out["source"] == src


def test_every_kit_has_captures():
    for src in kits.ALL:
        assert sourcekit.captures(src), f"tests/captures/{src}/ has no .http capture"


# ---- helpers -------------------------------------------------------------------------------------------------
@pytest.fixture
def env(tmp_path, clock):
    made = []

    def make(kit=kits.SIGNED, **over):
        e = Env(kit, tmp_path / f"e{len(made)}", clock, cfg_over=over or None)
        made.append(e)
        return e
    yield make
    for e in made:
        e.close()


def status(r) -> str:
    return json.loads(r.body)["status"]


# ---- order of operations -------------------------------------------------------------------------------------------
def test_order_of_operations_for_a_queued_event(env):
    e = env()
    r = e.send(e.signed())
    assert status(r) == "queued"
    assert e.spy.calls == ["verify", "ignore", "key", "dedup_id", "event_type", "attrs"]


def test_ignore_acks_without_enqueue(env):
    e = env()
    r = e.send(e.signed(kits.SIGNED.sample.set("X-Kit-Event", "ping")))
    assert (r.status, status(r)) == (200, "ignored")
    assert e.spy.calls == ["verify", "ignore"]
    assert e.batches() == []
    assert e.intake.counters.snapshot()["rejected"] == {("kitsigned", "ignored"): 1}


def test_verified_handshake_answers_and_never_enqueues(env):
    e = env(kits.STAMPED)
    r = e.send(e.signed(kits.STAMPED.handshake))
    doc = json.loads(r.body)
    assert r.status == 200 and doc["plainToken"] == "plain-" + SENTINEL
    import hashlib
    import hmac
    assert doc["encryptedToken"] == hmac.new(kits.STAMPED_SECRET.encode(), doc["plainToken"].encode(),
                                             hashlib.sha256).hexdigest()
    assert e.spy.calls == ["verify", "challenge"]
    assert e.batches() == []


def test_bad_signature_handshake_gets_no_token(env):
    e = env(kits.STAMPED)
    raw = e.signed(kits.STAMPED.handshake).set("X-Kit-Signature", "v0=" + "0" * 64)
    r = e.send(raw)
    assert (r.status, r.body, r.headers) == (401, b"", {})
    assert e.spy.calls == ["verify"]


def test_unauthenticated_challenge_gets_secret_free_mapping(env):
    e = env(kits.HEAD)
    seen = {}
    src = e.intake._snap.sources["kithead"]
    orig = src._challenge

    def spy(req, secrets):
        seen["type"] = type(secrets)
        with pytest.raises(P.SecretAccessDenied):
            secrets["secret"]
        return orig(req, secrets)
    src._challenge = spy
    r = e.send(kits.HEAD.unauth_handshake)
    assert (r.status, r.body) == (200, b"")
    assert seen["type"] is P.NoSecrets


def test_unauthenticated_source_still_verifies_events(env):
    e = env(kits.HEAD)
    r = e.send(kits.HEAD.sample)
    assert r.status == 401 and e.spy.calls == ["challenge", "verify"]
    r = e.send(e.signed())
    assert status(r) == "queued"


def test_401_is_empty_and_logged_once_without_details(env):
    e = env()
    r = e.send(kits.SIGNED.sample.set("X-Kit-Signature", "sha256=" + "0" * 64))
    assert (r.status, r.body, r.headers) == (401, b"", {})
    recs = [x for x in e.logs.records if x.getMessage() == "unverified request"]
    assert len(recs) == 1
    assert recs[0].source == "kitsigned" and recs[0].bytes == len(kits.SIGNED.sample.body)


def test_unknown_source_and_paths(env):
    e = env()
    for target in ("/hooks/nope", "/hooks/", "/hooks/kitsigned/x", "/other"):
        assert e.send(request("POST", target, {}, b"{}")).status == 404
    assert e.spy.calls == []


# ---- body cap ----------------------------------------------------------------------------------------------------
def test_cap_is_clamped_to_the_route_max_bytes(env):
    e = env(coalesce={"max_bytes": 4096})
    assert e.intake._snap.caps["kitsigned"] == 4096
    body = kits.SIGNED.sample.body + b" " * (4097 - len(kits.SIGNED.sample.body))
    r = e.send(e.signed(kits.SIGNED.sample.with_body(body)))
    assert r.status == 413 and e.spy.calls == []
    assert e.intake.counters.snapshot()["rejected"] == {("kitsigned", "too_large"): 1}


def test_explicit_source_cap(env):
    e = env(sources={"kitsigned": {"max_body_bytes": 300}})
    assert e.intake._snap.caps["kitsigned"] == 300


def test_content_length_must_be_numeric_and_complete(env):
    e = env()
    raw = e.signed()
    hs = [(k, v) for k, v in raw.headers] + [("Content-Length", "12x")]
    r = e.intake.handle("POST", raw.target, hs, io.BytesIO(raw.body))
    assert r.status == 400 and e.spy.calls == []
    hs = [(k, v) for k, v in raw.headers] + [("Content-Length", str(len(raw.body) + 5))]
    r = e.intake.handle("POST", raw.target, hs, io.BytesIO(raw.body))
    assert r.status == 400 and e.spy.calls == []


def test_chunked_counts_and_never_reads(env):
    e = env()

    class NoRead(io.BytesIO):
        def read(self, *a):
            raise AssertionError("the body was read")
    raw = e.signed()
    hs = list(raw.headers) + [("Transfer-Encoding", "chunked")]
    r = e.intake.handle("POST", raw.target, hs, NoRead())
    assert r.status == 411 and r.headers.get("Connection") == "close"
    # Transfer-Encoding wins over a Content-Length sent alongside it (no smuggling)
    hs = list(raw.headers) + [("Content-Length", str(len(raw.body))), ("Transfer-Encoding", "chunked")]
    r = e.intake.handle("POST", raw.target, hs, NoRead())
    assert r.status == 411 and e.spy.calls == []
    assert e.intake.counters.snapshot()["rejected"] == {("kitsigned", "chunked"): 2}


# ---- verify failures ------------------------------------------------------------------------------------------------
def test_verify_raising_answers_503_and_stores_nothing(env):
    """Deviation (recorded in the T3 Result): an exception inside verify() cannot be
    verified, so it is neither stored nor 401'd as a forgery; it is a 503 so a retrying
    sender comes back after the fix."""
    e = env()

    def boom(req, secrets):
        raise RuntimeError("bug " + SENTINEL)
    e.plugin.verify = boom
    r = e.send(e.signed())
    assert (r.status, r.headers.get("Retry-After"), r.body) == (503, "60", b"")
    assert e.batches() == []


def test_unreadable_secret_answers_503(env):
    e = env()
    os.unlink(e.secret_paths["secret"])
    r = e.send(e.signed())
    assert (r.status, r.headers.get("Retry-After")) == (503, "60")
    assert e.batches() == []


def test_secret_file_rotates_without_restart(env):
    e = env()
    with open(e.secret_paths["secret"], "w") as f:
        f.write("rotated-test-secret")
    assert e.send(e.signed()).status == 401
    raw = kits.sign_signed(kits.SIGNED.sample, {"secret": "rotated-test-secret"}, 0)
    assert status(e.send(raw)) == "queued"


# ---- quarantine -------------------------------------------------------------------------------------------------------
def test_quarantine_record(env):
    e = env()
    e.spy.broken = "attrs"
    raw = e.signed().set("Authorization", "Bearer x").set("Cookie", "c=1")
    raw = request(raw.method, raw.target + "?token=" + SENTINEL, raw.headers, raw.body)
    r = e.send(raw)
    assert (r.status, json.loads(r.body)) == (202, {"status": "quarantined"})
    b = e.batches()[0]
    assert (b["state"], b["destination"], b["key"], b["route"], b["last_class"]) == \
        ("dead", "quarantine", "quarantine", "kitsigned", "quarantine")
    assert b["last_error"] == "plugin error: RuntimeError in attrs"
    ev = e.events()[0]
    h = json.loads(ev["headers"])
    assert h[":method"] == "POST" and h[":path"] == "/hooks/kitsigned", "no query string is stored"
    assert h["X-Kit-Event"] == "push"
    assert not {"x-kit-signature", "authorization", "cookie"} & {k.lower() for k in h}
    assert bytes(ev["raw"]) == raw.body
    assert e.intake.counters.snapshot()["quarantined"] == {"kitsigned": 1}


def test_non_json_body_is_quarantined(env):
    e = env()
    r = e.send(e.signed(kits.SIGNED.sample.with_body(b"not json " + SENTINEL.encode())))
    assert r.status == 202
    assert e.batches()[0]["last_error"] == "plugin error: BadPayload in payload"


def test_no_destination_is_quarantined(env):
    e = env()
    e.intake._snap.sources["kitsigned"]._action = None
    r = e.send(e.signed())
    assert r.status == 202
    assert e.batches()[0]["last_error"] == "plugin error: NoDestination in route"


def test_invalid_key_from_plugin_is_quarantined(env):
    e = env()
    e.plugin.key = lambda payload, req: {"not": "a string"}
    r = e.send(e.signed())
    assert r.status == 202 and e.batches()[0]["destination"] == "quarantine"


def test_quarantine_under_low_disk_is_503(env):
    e = env()
    e.spy.broken = "key"
    e.statvfs.free = 0
    r = e.send(e.signed())
    assert r.status == 503 and e.batches() == []


def test_reclassify_after_fix(env):
    e = env()
    e.spy.broken = "key"
    assert e.send(e.signed()).status == 202
    bid = e.batches()[0]["id"]
    e.spy.broken = None
    fn = I.reclassifier(e.cfg, loader=e._load, now=e.clock)
    res = e.store.reclassify_quarantine(bid, fn)
    assert len(res) == 1
    q = e.store.batch(bid)
    assert (q["state"], q["result"]) == ("delivered", "reclassified")
    new = e.store.batch(res[0].batch_id)
    assert (new["destination"], new["route"], new["key"]) == ("hermes:kit", "kitsigned", "octo/widgets:push")
    assert json.loads(e.events()[-1]["headers"]) == {"X-Kit-Event": "push", "X-Kit-Delivery": "d-1"}


def test_reclassify_still_broken_changes_nothing(env):
    e = env()
    e.spy.broken = "key"
    e.send(e.signed())
    bid = e.batches()[0]["id"]
    with pytest.raises(P.PluginError):
        e.store.reclassify_quarantine(bid, I.reclassifier(e.cfg, loader=e._load, now=e.clock))
    assert e.store.batch(bid)["state"] == "dead"


# ---- route resolution ------------------------------------------------------------------------------------------------
def test_route_hint_header_matches_routes(env):
    """``Source.attrs`` adds ``route_hint`` (from ``X-Hookspool-Route``, set by the proxy) so
    ``[[routes]] match = {route_hint = …}`` can pick a route per provider-side URL."""
    e = env(routes=[{"name": "kit-ci", "source": "kitsigned", "match": {"route_hint": "ci"},
                     "destination": "hermes:kit-ci"},
                    {"name": "kit-repo", "source": "kitsigned", "match": {"repo": "octo/widgets"}}])
    r = e.send(e.signed(kits.SIGNED.sample.set(P.ROUTE_HEADER, "ci")))
    assert status(r) == "queued"
    b = e.batches()[-1]
    assert (b["route"], b["destination"]) == ("kit-ci", "hermes:kit-ci")
    raw = kits.SIGNED.sample.set("X-Kit-Delivery", "d-2")
    assert status(e.send(e.signed(raw))) == "queued"
    b = e.batches()[-1]
    assert (b["route"], b["destination"]) == ("kit-repo", "hermes:kit")


def test_quarantine_headers_strip_auth_before_the_store():
    src = P.Source("kitsigned", kits.SIGNED.module)
    req = P.Request("POST", "/hooks/kitsigned?token=x", [("X-Kit-Signature", "s"), ("authorization", "a"),
                                                        ("Proxy-Authorization", "p"), ("Cookie", "c"),
                                                        (":authority", "h"), ("X-Kit-Event", "push")], b"")
    assert src.quarantine_headers(req) == {":method": "POST", ":path": "/hooks/kitsigned", "X-Kit-Event": "push"}


def test_kept_headers_never_include_auth_headers(env):
    e = env(sources={"kitsigned": {"keep_headers": ["X-Kit-Event", "X-Kit-Signature", "Authorization"]}})
    e.send(e.signed().set("Authorization", "Bearer x"))
    assert json.loads(e.events()[0]["headers"]) == {"X-Kit-Event": "push"}


# ---- spool limits ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("msg,reason", [("database or disk is full", "spool_full"),
                                        ("database is locked", "busy")])
def test_503_on_spool_error(env, msg, reason):
    e = env()

    def fail(*a, **k):
        raise SpoolError(msg)
    e.store.enqueue = fail
    r = e.send(e.signed())
    assert (r.status, r.headers.get("Retry-After"), r.body) == (503, "60", b"")
    assert e.intake.counters.snapshot()["rejected"] == {("kitsigned", reason): 1}


def test_503_on_min_free_and_statvfs_cache(env, clock):
    e = env()
    calls = []
    orig = e.statvfs.__call__

    def counting(path):
        calls.append(path)
        return orig(path)
    e.intake.statvfs = counting
    assert status(e.send(e.signed())) == "queued"
    e.statvfs.free = 10
    r = e.send(e.signed(kits.SIGNED.sample.set("X-Kit-Delivery", "d-2")))
    assert r.status == 200, "statvfs is cached for 5 s"
    clock.advance(6)
    r = e.send(e.signed(kits.SIGNED.sample.set("X-Kit-Delivery", "d-3")))
    assert (r.status, r.headers.get("Retry-After")) == (503, "60")
    assert len(calls) == 2
    code, doc = e.intake.health()
    assert code == 503 and doc["ok"] is False
    assert "disk free below limits.min_free_bytes" in doc["degraded"]
    e.statvfs.free = 10 ** 12
    clock.advance(6)
    assert e.intake.health()[0] == 200


# ---- /healthz -------------------------------------------------------------------------------------------------------------
def test_healthz_write_probe_cadence(env, clock):
    e = env()
    n = []
    orig = e.store.meta_set
    e.store.meta_set = lambda v: (n.append(v), orig(v))[1]
    code, doc = e.intake.health()
    assert code == 200 and doc["ok"] is True and len(n) == 1
    assert doc["spool"]["writable_checked_age_s"] == 0.0
    clock.advance(10)
    e.intake.health()
    clock.advance(10)
    code, doc = e.intake.health()
    assert len(n) == 1 and doc["spool"]["writable_checked_age_s"] == 20.0
    clock.advance(11)
    e.intake.health()
    assert len(n) == 2, "a new probe after 30 s"
    # a recent enqueue proves writability: no probe
    clock.advance(40)
    e.send(e.signed())
    clock.advance(5)
    e.intake.health()
    assert len(n) == 2


def test_healthz_unwritable_spool(env, clock):
    e = env()
    n = []

    def fail(v):
        n.append(v)
        raise SpoolError("disk I/O error")
    e.store.meta_set = fail
    code, doc = e.intake.health()
    assert code == 503 and "spool unwritable" in doc["degraded"]
    clock.advance(10)
    assert e.intake.health()[0] == 503
    clock.advance(10)
    assert e.intake.health()[0] == 503
    assert len(n) == 1, "a failing probe is not retried more than once per 30 s"
    clock.advance(11)
    e.intake.health()
    assert len(n) == 2
    r = e.intake.handle("GET", "/healthz", [], io.BytesIO(b""))
    assert r.status == 503 and json.loads(r.body)["ok"] is False


def test_healthz_soft_degraded_stays_200(env):
    e = env()
    e.spy.broken = "key"
    e.send(e.signed())
    code, doc = e.intake.health()
    assert code == 200 and doc["ok"] is True
    assert doc["batches"]["quarantined"] == 1 or doc["degraded"], doc


def test_healthz_and_metrics_methods(env):
    e = env()
    assert e.intake.handle("POST", "/healthz", [("Content-Length", "0")], io.BytesIO()).status == 405
    assert e.intake.handle("GET", "/healthz?x=1", [], io.BytesIO()).status == 200


# ---- /metrics ----------------------------------------------------------------------------------------------------------------
def test_metrics_text(env):
    e = env()
    e.send(e.signed())
    e.send(e.signed())                                                      # duplicate
    e.send(kits.SIGNED.sample)                                              # unverified
    e.spy.broken = "key"
    e.send(e.signed(kits.SIGNED.sample.set("X-Kit-Delivery", "d-9")))       # quarantined
    m = e.intake.metrics_text()
    lines = m.splitlines()
    for line in ('hookspool_events_received_total{source="kitsigned"} 2',
                 'hookspool_events_duplicate_total{source="kitsigned"} 1',
                 'hookspool_events_quarantined_total{source="kitsigned"} 1',
                 'hookspool_requests_rejected_total{source="kitsigned",reason="unverified"} 1',
                 'hookspool_batches{state="quarantined"} 1',
                 'hookspool_batches{state="dead"} 0',
                 'hookspool_batches{state="open"} 1'):
        assert line in lines, (line, m)
    for name in ("hookspool_attempts_30d", "hookspool_batches_delivered_7d", "hookspool_destination_state",
                 "hookspool_disk_free_bytes", "hookspool_spool_bytes", "hookspool_issues_open",
                 "hookspool_source_last_event_age_seconds"):
        assert f"# TYPE {name} " in m, name
    assert "# TYPE hookspool_events_received_total counter" in lines
    assert "# TYPE hookspool_batches gauge" in lines
    for line in lines:
        assert line.startswith("# TYPE ") or " " in line
    assert SENTINEL not in m
    r = e.intake.handle("GET", "/metrics", [], io.BytesIO())
    assert r.status == 200 and r.headers["Content-Type"].startswith("text/plain")


def test_metrics_label_escaping():
    assert I._esc('a"b\\c\nd') == 'a\\"b\\\\c\\nd'
    assert I._num(3.0) == "3" and I._num(True) == "1" and I._num(0.25) == "0.25"


# ---- logs ---------------------------------------------------------------------------------------------------------------
def test_logs_never_carry_bodies_secrets_or_queries(env, monkeypatch):
    monkeypatch.setenv("HOOKSPOOL_LOG", "json")
    e = env()
    target = "/hooks/kitsigned?token=" + SENTINEL
    for raw in (e.signed(), kits.SIGNED.sample, e.signed(kits.SIGNED.sample.with_body(b"x" + SENTINEL.encode()))):
        e.send(request(raw.method, target, raw.headers, raw.body))
    e.spy.broken = "key"
    e.send(e.signed(kits.SIGNED.sample.set("X-Kit-Delivery", "d-q")))
    text = e.logs.rendered()
    assert e.logs.records
    assert SENTINEL not in text and kits.SIGNED_SECRET not in text and "token=" not in text
    for line in text.splitlines():
        if line.startswith("{"):
            doc = json.loads(line)
            assert set(doc) <= {"ts", "level", "component", "msg", "error", *hs_logs.FIELDS}


def test_log_fields_whitelist_and_exception_class_only():
    assert hs_logs.fields(source="s", body="x", headers={"a": 1}, bytes=None) == {"source": "s"}
    rec = logging.LogRecord("hookspool.intake", logging.ERROR, __file__, 1, "boom", (), None)
    try:
        raise ValueError(SENTINEL)
    except ValueError:
        import sys
        rec.exc_info = sys.exc_info()
    out = hs_logs.JsonFormatter().format(rec) + hs_logs.TextFormatter().format(rec)
    assert SENTINEL not in out and "ValueError" in out


def test_forwarded_for_trusted_only_from_loopback(env):
    e = env()
    e.send(kits.SIGNED.sample.set("X-Forwarded-For", "203.0.113.9, 10.0.0.1"), peer="127.0.0.1")
    e.send(kits.SIGNED.sample.set("X-Forwarded-For", "203.0.113.9"), peer="192.0.2.5")
    reasons = [r.reason for r in e.logs.records if r.getMessage() == "unverified request"]
    assert reasons == ["remote 203.0.113.9", "remote 192.0.2.5"]


# ---- the socket layer ------------------------------------------------------------------------------------------------------
@pytest.fixture
def server(env):
    started = []

    def start(kit=kits.SIGNED):
        e = env(kit)
        srv = I.make_server(e.intake, "127.0.0.1:0")
        t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        t.start()
        started.append(srv)
        return e, srv.server_address[1]
    yield start
    for srv in started:
        srv.shutdown()
        srv.server_close()


def test_socket_round_trip(server):
    e, port = server()
    raw = e.signed()
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", raw.target, body=raw.body, headers=dict(raw.headers))
    r = c.getresponse()
    assert (r.status, json.loads(r.read())) == (200, {"status": "queued"})
    c.request("POST", raw.target, body=raw.body, headers=dict(raw.headers))       # keep-alive
    r = c.getresponse()
    assert json.loads(r.read()) == {"status": "duplicate"}
    c.request("GET", "/healthz")
    r = c.getresponse()
    assert r.status == 200 and json.loads(r.read())["ok"] is True
    c.request("GET", "/metrics")
    r = c.getresponse()
    assert r.status == 200 and r.read().startswith(b"# TYPE ")
    c.request("POST", raw.target, body=raw.body + b" ", headers=dict(raw.headers))
    r = c.getresponse()
    assert (r.status, r.read()) == (401, b"")
    c.close()


def test_socket_chunked_is_411_and_closes(server):
    e, port = server()
    raw = e.signed()
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest("POST", raw.target)
    for k, v in raw.headers:
        c.putheader(k, v)
    c.putheader("Transfer-Encoding", "chunked")
    c.endheaders()
    c.send(b"%x\r\n%s\r\n0\r\n\r\n" % (len(raw.body), raw.body))
    r = c.getresponse()
    assert r.status == 411 and r.getheader("Connection", "").lower() == "close"
    r.read()
    c.close()
    assert e.spy.calls == [] and e.batches() == []


def test_socket_head_handshake(server):
    e, port = server(kits.HEAD)
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("HEAD", "/hooks/kithead")
    r = c.getresponse()
    assert r.status == 200 and r.getheader("Content-Length") == "0"
    r.read()
    c.close()


def test_parse_listen_is_loopback_only():
    assert I.parse_listen("127.0.0.1:8650") == ("tcp", ("127.0.0.1", 8650))
    assert I.parse_listen("localhost:1") == ("tcp", ("127.0.0.1", 1))
    assert I.parse_listen("[::1]:2") == ("tcp", ("::1", 2))
    assert I.parse_listen("unix:/run/x.sock") == ("unix", "/run/x.sock")
    for bad in ("0.0.0.0:80", "192.0.2.1:80", "example.test:80", "8650", "127.0.0.1:x"):
        with pytest.raises(ValueError):
            I.parse_listen(bad)


# ---- plugin.py units ---------------------------------------------------------------------------------------------------------
def test_request_view():
    req = P.Request("post", "/hooks/g?token=" + SENTINEL + "&a=1", [("X-A", "1"), ("x-a", "2"),
                                                                     (P.ROUTE_HEADER, "ci")], b'{"k": 1}')
    assert req.method == "POST" and req.path == "/hooks/g" and req.query == {"token": SENTINEL, "a": "1"}
    assert req.header("x-A") == "1" and req.header("missing") == ""
    assert req.route_hint == "ci" and SENTINEL not in repr(req)
    assert req.json() == {"k": 1} and req.json() is req.json()
    with pytest.raises(P.BadPayload):
        P.Request("POST", "/", [], b"{").json()
    assert P.Request("POST", "/", [], b"").json() is None


def test_source_wrapper_requires_mandatory_names():
    for missing in ("verify", "key", "auth_headers"):
        m = types.ModuleType("m")
        m.verify = lambda r, s: True
        m.key = lambda p, r: "k"
        m.auth_headers = ("X-S",)
        delattr(m, missing)
        with pytest.raises(P.PluginError):
            P.Source("m", m)
    m = types.ModuleType("m")
    m.verify, m.key, m.auth_headers = (lambda r, s: 1), (lambda p, r: "k"), "X-S"
    src = P.Source("m", m)
    assert src.verify(P.Request("POST", "/", [], b""), {}) is False, "only `True` verifies"
    assert src.auth_headers == ("X-S",)


def test_no_secrets_raises_everywhere():
    ns = P.NoSecrets()
    for op in (lambda: ns["a"], lambda: list(ns), lambda: len(ns), lambda: "a" in ns):
        with pytest.raises(P.SecretAccessDenied):
            op()


def test_secrets_resolve_at_access(tmp_path):
    p = tmp_path / "s"
    p.write_text("one")
    s = P.Secrets({"a": {"file": str(p)}}, "src")
    assert s["a"] == "one"
    p.write_text("two")
    assert s["a"] == "two"
    with pytest.raises(P.SecretError) as ei:
        s["b"]
    assert "two" not in str(ei.value) and "two" not in repr(s)


def test_dedup_helpers():
    req = P.Request("POST", "/", [("X-D", "abc")], b"body")
    assert P.dedup.header("X-D")({}, req) == "abc"
    assert P.dedup.header("X-E")({}, req) is None
    assert P.dedup.field("a.b.0")({"a": {"b": [7]}}, req) == "7"
    assert P.dedup.field("a.c")({"a": {}}, req) is None
    import hashlib
    assert P.dedup.body_sha256({}, req) == hashlib.sha256(b"body").hexdigest()


def test_capture_format_round_trip():
    raw = P.RawRequest("POST", "/hooks/x?q=1", [("X-Sig", "s"), ("Content-Type", "application/json")],
                       b'{"a":1}\n\n', {"now": "5", "expect": "queued"})
    back = P.parse_capture(P.format_capture(raw))
    assert (back.method, back.target, back.body, back.meta) == (raw.method, raw.target, raw.body, raw.meta)
    assert back.header("x-sig") == "s" and back.header("content-length") == str(len(raw.body))
    crlf = b"POST /hooks/x HTTP/1.1\r\nX-A: 1\r\n\r\nbody\r\n"
    assert P.parse_capture(crlf).body == b"body" and P.parse_capture(crlf).header("X-A") == "1"
    with pytest.raises(ValueError):
        P.parse_capture(b"\n\n")


# ---- `hookspool source test` and replay through the CLI -----------------------------------------------------------------
def _toml_config(tmp_path, kit) -> str:
    sec = tmp_path / "kit.secret"
    sec.write_text(kit.secrets["secret"])
    os.chmod(sec, 0o600)
    extra = "".join(f'{k} = "{v}"\n' for k, v in kit.source_cfg.items())
    p = tmp_path / "hookspool.toml"
    p.write_text(f'[spool]\nhome = "{tmp_path / "home"}"\n\n[alerts]\ndry = true\n\n'
                 f'[sources.{kit.name}]\nmodule = "{kit.module.__name__}"\n{extra}'
                 f'secrets = {{ secret = {{ file = "{sec}" }} }}\n')
    return str(p)


@pytest.mark.parametrize("src,path", CAPTURE_FILES, ids=[f"{s}/{os.path.basename(p)}" for s, p in CAPTURE_FILES])
def test_source_test_cli(src, path, tmp_path, capsys):
    kit = kits.ALL[src]
    cfg = _toml_config(tmp_path, kit)
    with open(path, "rb") as f:
        expect = P.parse_capture(f.read()).meta["expect"]
    rc = main(["--config", cfg, "source", "test", src, "--request", path])
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert doc["result"] == expect
    assert rc == (0 if expect in ("queued", "ignored", "challenge") else 1)
    assert SENTINEL not in out
    if expect == "queued":
        assert doc["destination"] == kit.module.action and doc["key"]


def test_source_test_cli_now_override(tmp_path, capsys):
    path = os.path.join(sourcekit.CAPTURES, "kitstamped", "meeting_ended.http")
    cfg = _toml_config(tmp_path, kits.STAMPED)
    assert main(["--config", cfg, "source", "test", "kitstamped", "--request", path, "--now", str(T0 + 301)]) == 1
    assert json.loads(capsys.readouterr().out)["result"] == "unverified"


def test_source_test_cli_errors(tmp_path, capsys):
    cfg = _toml_config(tmp_path, kits.SIGNED)
    assert main(["--config", cfg, "source", "test", "kitsigned", "--request", str(tmp_path / "none.http")]) == 2
    path = sourcekit.captures("kitsigned")[0]
    assert main(["--config", cfg, "source", "test", "unknown", "--request", path]) == 2
    capsys.readouterr()


def test_replay_reclassifies_through_the_cli(tmp_path, capsys, clock, make_store):
    cfg_path = _toml_config(tmp_path, kits.SIGNED)
    cfg = hs_config.load_config(cfg_path)
    st = make_store(cfg, path=hs_config.db_path(cfg))
    raw = kits.sign_signed(kits.SIGNED.sample, kits.SIGNED.secrets, T0)
    _, bid = st.quarantine("kitsigned", raw.body, {":method": "POST", ":path": "/hooks/kitsigned",
                                                   "X-Kit-Event": "push", "X-Kit-Delivery": "d-1"},
                           "plugin error: KeyError in key", auth_headers=("X-Kit-Signature",))
    st.close()
    assert main(["--config", cfg_path, "replay", str(bid)]) == 0
    assert "reclassified into 1 event" in capsys.readouterr().out
    st = make_store(cfg, path=hs_config.db_path(cfg))
    assert st.batch(bid)["result"] == "reclassified"
    assert st._read("SELECT COUNT(*) FROM batches WHERE destination='hermes:kit'")[0][0] == 1
