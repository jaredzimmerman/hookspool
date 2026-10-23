"""``hermes:<route>`` destination (C3, §6.4, §6.5, §14.2): request shape, signature, every
status and body ``status`` -> outcome/notice, a fresh secret per attempt, time bounds, and the
dispatcher round trip."""
import json
import threading
import time

import pytest

from _httpstub import Stub, doc_for, encode, make_ctx, verify_legacy, verify_standard_webhooks
from conftest import SENTINEL, write_subs
from hookspool.destinations import build
from hookspool.destinations.hermes import HermesDestination


@pytest.fixture
def stub():
    made = []

    def start():
        s = Stub()
        made.append(s)
        return s
    yield start
    for s in made:
        s.close()


@pytest.fixture
def mk(make_config, stub):
    """(destination, stub) for ``hermes:<route>`` against a live stub; ``over`` merges config."""
    def make(route="test-route", over=None, spec=None):
        s = stub()
        cfg = make_config(port=s.port, **(over or {}))
        if spec:
            cfg["destinations"] = {f"hermes:{route}": spec}
        d = build(cfg, f"hermes:{route}")
        assert isinstance(d, HermesDestination), d
        return d, s
    return make


def go(d, doc=None, delivery_id="hs_i_7_1", ctx=None):
    doc = doc or doc_for()
    return d.deliver(doc, encode(doc), delivery_id, ctx or make_ctx(wall=True))


# ---- type properties -------------------------------------------------------------------------
def test_type_properties(mk):
    d, _ = mk()
    assert d.idempotency == "per-attempt" and d.max_inflight == 2
    assert d.max_body_bytes == 1048576 and d.timeout_s == 5.0       # make_config sets hermes.timeout_s = 5


def test_max_body_bytes_follows_config(mk):
    d, _ = mk(over={"hermes": {"max_body_bytes": 2000000}})
    assert d.max_body_bytes == 2000000
    d, _ = mk(spec={"max_body_bytes": 12345, "timeout_s": 9})
    assert d.max_body_bytes == 12345 and d.timeout_s == 9.0


# ---- request shape and signature ---------------------------------------------------------------
def test_posts_to_the_profile_path_with_verified_headers(mk, subs_path):
    d, s = mk()
    doc = doc_for(3)
    ctx = make_ctx(batch_id=7, attempt=2, wall=True)
    out = d.deliver(doc, encode(doc), "hs_inst_7_55", ctx)
    assert (out.kind, out.status) == ("ok", 200) and out.notice is None
    r = s.requests[0]
    assert r["path"] == "/p/test-profile/webhooks/test-route"
    assert r["body"] == encode(doc)
    h = r["headers"]
    assert verify_standard_webhooks(h, r["body"], "test-secret-A", now=time.time())
    assert verify_legacy(h, r["body"], "test-secret-A")
    assert h["webhook-id"] == "hs_inst_7_55"
    assert h["X-Hookspool-Batch"] == "7" and h["X-Hookspool-Attempt"] == "2"
    assert h["Content-Type"] == "application/json" and h["User-Agent"].startswith("hookspool/")
    assert "X-GitHub-Event" not in h              # no event_header configured: no per-source default


def test_route_without_profile_uses_the_bare_path(mk):
    d, s = mk(route="plain-route")
    go(d)
    assert s.requests[0]["path"] == "/webhooks/plain-route"
    assert verify_standard_webhooks(s.requests[0]["headers"], s.requests[0]["body"], "test-secret-plain",
                                    now=time.time())


def test_x_github_event_comes_from_config_not_from_the_source_name(mk):
    d, s = mk(over={"sources": {"github": {"module": "x", "event_header": "X-GitHub-Event"}}})
    go(d, doc_for(source="gmail", event_type="push"))
    go(d, doc_for(source="github", event_type=""))
    go(d, doc_for(source="github", event_type="push"))
    assert "X-GitHub-Event" not in s.requests[0]["headers"]
    assert "X-GitHub-Event" not in s.requests[1]["headers"]
    assert s.requests[2]["headers"]["X-GitHub-Event"] == "push"
    d2, s2 = mk(over={"sources": {"github": {"module": "x"}}})     # the name alone sets nothing
    go(d2, doc_for(source="github", event_type="push"))
    assert "X-GitHub-Event" not in s2.requests[0]["headers"]


@pytest.mark.parametrize("value", ["a\nb", "a\r\nX-Injected: 1", "\u4e8b\u4ef6", "tab\x00nul", "\x85"])
def test_an_event_type_that_cannot_go_on_the_wire_is_left_out_not_a_transport_loop(mk, value):
    """T4 round 2 note 1: http.client refused such a header before sending, which came back as a
    transport failure on every attempt (breaker trip, poison, and again on every replay)."""
    d, s = mk(over={"sources": {"gh2": {"module": "x", "event_header": "X-Event"}}})
    out = go(d, doc_for(source="gh2", event_type=value))
    assert out.kind == "ok", out
    assert "X-Event" not in s.requests[0]["headers"] and "X-Injected" not in s.requests[0]["headers"]


@pytest.mark.parametrize("name", ["webhook-signature", "Content-Type", "X-HUB-SIGNATURE-256", "x-hookspool-batch",
                                  "Host", "bad name", "x" * 65])
def test_event_header_cannot_name_a_header_hookspool_sets(mk, name):
    d, s = mk(over={"sources": {"gh2": {"module": "x", "event_header": name}}})
    assert go(d, doc_for(source="gh2", event_type="push")).kind == "ok"
    h = s.requests[0]["headers"]          # a later duplicate would overwrite ours in this dict
    assert h["Content-Type"] == "application/json" and h["webhook-signature"].startswith("v1,")
    assert "push" not in h.values()


def test_event_header_is_declared_by_the_source_not_its_name(mk):
    d, s = mk(over={"sources": {"gh2": {"module": "x", "event_header": "X-GitHub-Event"},
                                "other": {"module": "x"}}})
    go(d, doc_for(source="gh2", event_type="issues"))
    go(d, doc_for(source="other", event_type="issues"))
    assert s.requests[0]["headers"]["X-GitHub-Event"] == "issues"
    assert "X-GitHub-Event" not in s.requests[1]["headers"]


def test_subscriptions_file_with_a_bom_is_accepted(mk, subs_path):
    d, s = mk()
    subs_path.write_bytes(b"\xef\xbb\xbf" + subs_path.read_bytes())
    assert go(d).kind == "ok" and len(s.requests) == 1


def test_legacy_signature_can_be_turned_off(mk):
    d, s = mk(over={"hermes": {"legacy_signature": False}})
    go(d)
    h = s.requests[0]["headers"]
    assert "X-Hub-Signature-256" not in h and "webhook-signature" in h


def test_whsec_route_secret_signs_with_the_decoded_key(mk, subs_path):
    # The key below is the published Standard Webhooks example, not a credential.
    write_subs(subs_path, {"test-route": {"secret": "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"}})  # hookspool-scan: allow
    d, s = mk()
    go(d)
    r = s.requests[0]
    assert verify_standard_webhooks(r["headers"], r["body"], "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw",
                                    now=time.time())


def test_unusual_route_and_profile_names_are_path_safe(make_config, stub, subs_path):
    write_subs(subs_path, {"a/b": {"secret": "s", "profile": "p q"}})
    s = stub()
    d = build(make_config(port=s.port), "hermes:a/b")
    go(d)
    assert s.requests[0]["path"] == "/p/p%20q/webhooks/a%2Fb"


# ---- fresh secret and profile per attempt ---------------------------------------------------------
def test_secret_and_profile_are_read_fresh_each_attempt(mk, subs_path):
    d, s = mk()
    go(d)
    write_subs(subs_path, {"test-route": {"secret": "rotated-secret-B", "profile": "other-profile"}})
    go(d)
    a, b = s.requests
    assert a["path"] == "/p/test-profile/webhooks/test-route" and b["path"] == "/p/other-profile/webhooks/test-route"
    assert verify_standard_webhooks(a["headers"], a["body"], "test-secret-A", now=time.time())
    assert not verify_standard_webhooks(b["headers"], b["body"], "test-secret-A", now=time.time())
    assert verify_standard_webhooks(b["headers"], b["body"], "rotated-secret-B", now=time.time())
    assert verify_legacy(b["headers"], b["body"], "rotated-secret-B")


def test_each_attempt_signs_with_its_own_id_and_timestamp(mk):
    d, s = mk()
    go(d, delivery_id="hs_i_7_1", ctx=make_ctx(now=1_800_000_000.0, attempt=1))
    go(d, delivery_id="hs_i_7_2", ctx=make_ctx(now=1_800_000_015.0, attempt=2))
    a, b = s.requests
    assert a["headers"]["webhook-id"] != b["headers"]["webhook-id"]
    assert a["headers"]["webhook-signature"] != b["headers"]["webhook-signature"]
    assert verify_standard_webhooks(b["headers"], b["body"], "test-secret-A", now=1_800_000_015.0)


# ---- subscriptions problems are config outcomes --------------------------------------------------
@pytest.mark.parametrize("subs", [
    None,                                                   # file missing
    "not json",                                             # file garbled
    {},                                                     # route unknown
    {"test-route": "nope"},                                 # route not a table
    {"test-route": {"profile": "p"}},                       # no secret
    {"test-route": {"secret": "   "}},                      # blank secret
    {"test-route": {"secret": 5}},                          # non-string secret
    {"test-route": {"secret": "s", "profile": 5}},          # bad profile
    {"test-route": {"secret": "whsec_!!!"}},                # unusable whsec
])
def test_subscriptions_problems_are_config(mk, subs_path, subs):
    d, s = mk()
    if subs is None:
        subs_path.unlink()
    elif isinstance(subs, str):
        subs_path.write_text(subs)
    else:
        write_subs(subs_path, subs)
    out = go(d)
    assert (out.kind, out.klass) == ("retry", "config")
    assert s.requests == [] and out.reason
    assert "whsec_!!!" not in out.reason + out.detail


def test_destination_without_a_route_is_config(make_config):
    d = HermesDestination("hermes:", {}, make_config())
    assert go(d).klass == "config"


# ---- every status ------------------------------------------------------------------------------
@pytest.mark.parametrize("code,kind,klass,word", [
    (200, "ok", None, None), (201, "ok", None, None), (202, "ok", None, None),
    (401, "retry", "config", "bad secret"), (403, "retry", "config", "forbidden"),
    (404, "retry", "config", "unknown route"), (405, "retry", "config", None), (410, "retry", "config", None),
    (400, "dead", "permanent", None), (413, "dead", "permanent", "too large"),
    (415, "dead", "permanent", None), (422, "dead", "permanent", None),
    (429, "retry", "throttle", None), (500, "retry", "transport", None), (502, "retry", "transport", None),
    (503, "retry", "transport", None), (504, "retry", "transport", None),
    (302, "retry", "config", "redirect"),
])
def test_status_to_outcome(mk, code, kind, klass, word):
    d, s = mk()
    s.respond({"status": code, "body": b'{"error":"x"}'})
    out = go(d)
    assert (out.kind, out.klass, out.status) == (kind, klass, code)
    if word:
        assert word in out.reason
    assert SENTINEL not in out.reason + out.detail


def test_429_and_503_carry_retry_after(mk):
    d, s = mk()
    s.respond({"status": 429, "headers": {"Retry-After": "42"}}, {"status": 503, "headers": {"Retry-After": "9"}},
              {"status": 503})
    a, b, c = go(d), go(d), go(d)
    assert (a.klass, a.retry_after_s) == ("throttle", 42.0)
    assert (b.klass, b.retry_after_s) == ("throttle", 9.0)
    assert c.klass == "transport"


def test_404_names_the_route_not_the_secret(mk):
    d, s = mk()
    s.respond({"status": 404})
    out = go(d)
    assert "test-route" in out.reason and "test-secret-A" not in out.reason + out.detail


@pytest.mark.parametrize("body,notice", [
    (b'{"status":"accepted","route":"r"}', None),
    (b'{"status":"delivered"}', None),
    (b'{"status":"duplicate","delivery_id":"x"}', None),          # admission, not an event to flag
    (b'{"status":"ignored","event":"push"}', "ignored"),
    (b'{"status":"ignored","reason":"filter","route":"r"}', "ignored"),
    (b'{"status":"coalesced","route":"r"}', "coalesced"),
    (b'{"status":"something-new"}', None),
    (b"not json at all", None), (b"", None), (b"[1,2]", None), (b'{"status":5}', None),
])
def test_2xx_body_status_to_notice(mk, body, notice):
    d, s = mk()
    s.respond({"status": 200, "body": body})
    out = go(d)
    assert out.kind == "ok" and out.klass is None and out.notice == notice


# ---- transport failures --------------------------------------------------------------------------
def test_refused_connection_is_transport(make_config):
    s = Stub()
    cfg = make_config(port=s.port)
    s.close()
    out = go(build(cfg, "hermes:test-route"))
    assert (out.kind, out.klass) == ("retry", "transport") and "unreachable" in out.reason


def test_unreachable_base_url_unset_is_config(make_config):
    cfg = make_config()
    cfg["hermes"]["base_url"] = ""
    out = go(build(cfg, "hermes:test-route"))
    assert out.klass == "config"


def test_stalled_gateway_returns_at_the_timeout(mk):
    d, s = mk(spec={"timeout_s": 0.4})
    hold = threading.Event()
    s.respond({"status": 200, "hold": hold})
    t0 = time.monotonic()
    out = go(d)
    hold.set()
    assert (out.klass, out.reason) == ("transport", "timeout") and time.monotonic() - t0 < 2.0


def test_stalled_gateway_returns_at_the_worker_deadline_not_the_timeout(mk):
    d, s = mk(spec={"timeout_s": 60})
    hold = threading.Event()
    s.respond({"status": 200, "hold": hold})
    ctx = make_ctx(wall=True, ttl=0.4)             # deadline in 0.4 s; timeout_s alone would allow 60 s
    t0 = time.monotonic()
    out = go(d, ctx=ctx)
    hold.set()
    assert (out.klass, out.reason) == ("transport", "deadline") and time.monotonic() - t0 < 2.0


def test_dripping_gateway_cannot_outlive_the_deadline(mk):
    d, s = mk(spec={"timeout_s": 60})
    s.respond({"status": 200, "drip": 0.1})
    t0 = time.monotonic()
    out = go(d, ctx=make_ctx(wall=True, ttl=0.5))
    assert out.klass == "transport" and time.monotonic() - t0 < 2.0


def test_no_time_left_does_not_send(mk):
    d, s = mk()
    out = go(d, ctx=make_ctx(wall=True, ttl=-1))
    assert (out.klass, out.reason) == ("transport", "deadline") and s.requests == []


def test_plain_text_http_to_a_tls_looking_port_is_not_a_crash(mk):
    d, s = mk()
    s.respond({"status": 200, "body": b"x"})
    d.base_url = d.base_url.replace("http://", "https://")        # speaking TLS to a plain server
    out = go(d)
    assert out.kind == "retry" and out.klass in ("transport", "config")


# ---- check ---------------------------------------------------------------------------------------
def test_check_reports_route_and_url_problems_without_secrets(mk, subs_path):
    d, _ = mk()
    assert d.check() == []
    write_subs(subs_path, {})
    assert any("unknown route" in p for p in d.check())
    write_subs(subs_path, {"test-route": {"secret": "whsec_!!!"}})
    probs = d.check()
    assert probs and not any("whsec_!!!" in p for p in probs)
    d.base_url = "ftp://x"
    assert any("base_url" in p for p in d.check())


# ---- through the dispatcher ----------------------------------------------------------------------
def test_dispatcher_round_trip_fresh_id_per_attempt_and_notice(rig, stub, clock):
    s = stub()
    r = rig({"port": s.port}, destination_factory=build)
    s.respond({"status": 502}, {"status": 200, "body": b'{"status":"coalesced"}'})
    a = r.enqueue(dest="hermes:test-route", n=1, event_type="push")
    assert r.d.drain_once() == 1
    clock.advance(30)
    assert r.d.drain_once() == 1
    b = r.batch(a.batch_id)
    assert b["state"] == "delivered" and b["attempts"] == 1      # the 502 was transport: uncounted
    ids = [q["headers"]["webhook-id"] for q in s.requests]
    atts = r.store.attempts(a.batch_id)
    inst = r.store.instance_id()
    assert ids == [f"hs_{inst}_{a.batch_id}_{x['id']}" for x in atts] and len(set(ids)) == 2
    bodies = [json.loads(q["body"]) for q in s.requests]
    assert bodies[0]["content_hash"] == bodies[1]["content_hash"] and [x["attempt"] for x in bodies] == [1, 2]
    assert all(verify_standard_webhooks(q["headers"], q["body"], "test-secret-A", now=clock()) for q in s.requests[1:])
    fps = {i["fingerprint"] for i in r.store.issue_list("open")}
    assert "delivery:hermes:test-route:ignored" in fps


def test_dispatcher_413_goes_dead_and_401_stays_retrying(rig, stub, clock):
    s = stub()
    r = rig({"port": s.port}, destination_factory=build)
    s.respond({"status": 413})
    a = r.enqueue(dest="hermes:test-route", key="a")
    r.d.drain_once()
    assert r.batch(a.batch_id)["state"] == "dead"
    s.respond({"status": 401})
    b = r.enqueue(dest="hermes:test-route", key="b")
    r.d.drain_once()
    bb = r.batch(b.batch_id)
    assert (bb["state"], bb["last_class"], bb["attempts"]) == ("retry", "config", 0)
