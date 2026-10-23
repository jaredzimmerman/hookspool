"""``unwrap = true`` on ``hermes`` and ``http`` (ARCHITECTURE §6.4.1): one request per event with the
stored payload as the body, per-event ids stable across attempts, a failure mid-batch retries the
whole batch in order, the breaker and the dead path as for a document, per-event progress on the
attempt row, the ``check`` rules, and no body in any log line or stored error."""
import json
import os
import time
from dataclasses import replace

import pytest

from _httpstub import Stub, doc_for, encode, make_ctx, verify_legacy, verify_standard_webhooks
from conftest import SENTINEL, write_subs
from hookspool import config as C
from hookspool.destinations import build
from hookspool.destinations.base import Outcome
from hookspool.destinations.hermes import HermesDestination
from hookspool.destinations.http import HttpDestination
from hookspool.destinations.wire import unwrap_deliver

DEST = "hermes:test-route"
UNWRAP = {"unwrap": True, "event_header": "X-GitHub-Event", "dedup_header": "X-GitHub-Delivery"}
DUP = {"status": 200, "body": b'{"status":"duplicate"}'}
OK = {"status": 200, "body": b'{"status":"accepted"}'}


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


def unwrap_rig(rig, s, spec=None, **over):
    return rig({"port": s.port, "destinations": {DEST: dict(UNWRAP if spec is None else spec)}, **over},
               destination_factory=build)


def enqueue_batch(r, clock, payloads, *, key="k", dest=DEST, types=None, dedups=None):
    """Several events in one batch (a 5 s window), sealed by advancing the clock past it."""
    out = []
    for i, p in enumerate(payloads):
        out.append(r.store.enqueue("src", key, dest, p, quiet_s=5, max_s=5,
                                   event_type=(types or [""] * len(payloads))[i],
                                   dedup_id=(dedups or [None] * len(payloads))[i]))
    assert len({x.batch_id for x in out}) == 1
    clock.advance(6)
    return out


def payloads(n=3):
    # non-ASCII text, nesting and key order: a re-encoding could differ, the stored bytes cannot
    return [{"zeta": 1, "action": "opened", "pull_request": {"title": f"café 事件 #{i}", "n": i},
             "note": SENTINEL, "alpha": [1.5, None, True]} for i in range(n)]


# ---- type properties --------------------------------------------------------------------------
def test_unwrap_is_off_by_default_and_per_event_when_on(make_config):
    assert build(make_config(), DEST).unwrap is False
    d = build(make_config(destinations={DEST: dict(UNWRAP)}), DEST)
    assert isinstance(d, HermesDestination) and d.unwrap and d.idempotency == "per-event"
    h = build(make_config(destinations={"out": {"type": "http", "url": "https://example.invalid/h",
                                                "secret": {"env": "X"}, "unwrap": True}}), "out")
    assert isinstance(h, HttpDestination) and h.unwrap and h.idempotency == "per-event"


# ---- bodies, headers, ids -----------------------------------------------------------------------
def test_each_event_is_its_own_request_with_the_stored_payload_byte_for_byte(rig, stub, clock):
    s = stub()
    r = unwrap_rig(rig, s)
    evs = enqueue_batch(r, clock, payloads(3), types=["pull_request", "pull_request", "issues"],
                        dedups=["gh-1", None, "gh-3"])
    bid = evs[0].batch_id
    assert r.d.drain_once() == 1
    assert r.batch(bid)["state"] == "delivered"
    stored = r.store.stored_payloads(bid)
    assert [q["body"] for q in s.requests] == [p for _, p in stored]
    assert [q["body"] for q in s.requests] == [json.dumps(p, separators=(",", ":"), ensure_ascii=False).encode()
                                               for p in payloads(3)]
    assert all(q["path"] == "/p/test-profile/webhooks/test-route" for q in s.requests)
    inst = r.store.instance_id()
    for q, ev, et, dd in zip(s.requests, evs, ["pull_request", "pull_request", "issues"], ["gh-1", None, "gh-3"]):
        h = q["headers"]
        assert h["webhook-id"] == f"hs_{inst}_{bid}_{ev.event_id}"
        assert verify_standard_webhooks(h, q["body"], "test-secret-A", now=clock())
        assert verify_legacy(h, q["body"], "test-secret-A")
        assert h["X-GitHub-Event"] == et
        assert h.get("X-GitHub-Delivery") == dd
        assert h["X-Hookspool-Batch"] == str(bid) and h["X-Hookspool-Attempt"] == "1"
    att = r.store.attempts(bid)
    assert [(a["outcome"], a["events_delivered"], a["failed_event_id"]) for a in att] == [("ok", 3, None)]
    assert att[0]["delivery_id"] == f"hs_{inst}_{bid}"


def test_a_document_destination_is_unchanged(rig, stub, clock):
    s = stub()
    r = rig({"port": s.port}, destination_factory=build)
    evs = enqueue_batch(r, clock, payloads(2))
    r.d.drain_once()
    assert len(s.requests) == 1 and json.loads(s.requests[0]["body"])["count"] == 2
    att = r.store.attempts(evs[0].batch_id)
    assert (att[0]["events_delivered"], att[0]["failed_event_id"]) == (None, None)


def test_the_source_event_header_is_the_hermes_fallback(rig, stub, clock):
    s = stub()
    r = unwrap_rig(rig, s, spec={"unwrap": True},
                   sources={"src": {"module": "x", "event_header": "X-Event-Kind"}})
    enqueue_batch(r, clock, payloads(1), types=["push"])
    r.d.drain_once()
    h = s.requests[0]["headers"]
    assert h["X-Event-Kind"] == "push" and "X-GitHub-Delivery" not in h


@pytest.mark.parametrize("value", ["a\nb", "事", "x\x00y"])
def test_a_header_value_that_cannot_go_on_the_wire_is_left_out(rig, stub, clock, value):
    s = stub()
    r = unwrap_rig(rig, s)
    evs = enqueue_batch(r, clock, payloads(1), types=[value], dedups=[value])
    r.d.drain_once()
    assert r.batch(evs[0].batch_id)["state"] == "delivered"
    h = s.requests[0]["headers"]
    assert "X-GitHub-Event" not in h and "X-GitHub-Delivery" not in h


# ---- failure mid-batch --------------------------------------------------------------------------
def test_mid_batch_failure_retries_the_batch_in_order_with_stable_ids(rig, stub, clock):
    s = stub()
    r = unwrap_rig(rig, s)
    evs = enqueue_batch(r, clock, payloads(3))
    bid, ids = evs[0].batch_id, [e.event_id for e in evs]
    s.respond(OK, {"status": 502})                       # event 2 fails: transport
    assert r.d.drain_once() == 1
    b = r.batch(bid)
    assert (b["state"], b["last_class"], b["attempts"]) == ("retry", "transport", 0)
    assert len(s.requests) == 2                          # event 3 was never sent past the failure
    clock.advance(60)
    s.respond(DUP, OK, OK)                               # the accepted event is answered duplicate
    assert r.d.drain_once() == 1
    assert r.batch(bid)["state"] == "delivered"
    inst = r.store.instance_id()
    sent = [q["headers"]["webhook-id"] for q in s.requests]
    want = [f"hs_{inst}_{bid}_{i}" for i in ids]
    assert sent == [want[0], want[1], want[0], want[1], want[2]]
    # never out of order: in each attempt the events go out in id order from the first
    assert [q["headers"]["X-Hookspool-Attempt"] for q in s.requests] == ["1", "1", "2", "2", "2"]
    att = r.store.attempts(bid)
    assert [(a["outcome"], a["klass"], a["events_delivered"], a["failed_event_id"]) for a in att] == [
        ("retry", "transport", 1, ids[1]), ("ok", None, 3, None)]
    assert f"event {ids[1]} (2 of 3)" in att[0]["error"] and "1 of 3 delivered" in att[0]["error"]


def test_a_rejected_event_kills_the_batch_after_the_accepted_ones(rig, stub, clock):
    s = stub()
    r = unwrap_rig(rig, s)
    evs = enqueue_batch(r, clock, payloads(3))
    s.respond(OK, {"status": 422})
    r.d.drain_once()
    b = r.batch(evs[0].batch_id)
    assert (b["state"], b["last_class"]) == ("dead", "permanent") and len(s.requests) == 2
    a = r.store.attempts(evs[0].batch_id)[-1]
    assert (a["events_delivered"], a["failed_event_id"]) == (1, evs[1].event_id)
    assert "delivery:hermes:test-route:permanent" in {i["fingerprint"] for i in r.store.issue_list("open")}


def test_a_bad_secret_mid_batch_is_a_config_retry(rig, stub, clock):
    s = stub()
    r = unwrap_rig(rig, s)
    evs = enqueue_batch(r, clock, payloads(2))
    s.respond(OK, {"status": 401})
    r.d.drain_once()
    b = r.batch(evs[0].batch_id)
    assert (b["state"], b["last_class"], b["attempts"]) == ("retry", "config", 0)
    assert "config:delivery:hermes:test-route" in {i["fingerprint"] for i in r.store.issue_list("open")}


def test_the_breaker_trips_on_unwrapped_transport_failures(rig, stub, clock):
    s = stub()
    r = unwrap_rig(rig, s)
    s.default = {"status": 502}
    bids = []
    for k in ("a", "b", "c"):
        bids.append(r.enqueue(key=k, dest=DEST, n=1).batch_id)
    r.d.drain_once()
    assert r.dest(DEST)["state"] == "open"
    assert {r.batch(b)["state"] for b in bids} == {"retry"}
    assert all(r.store.attempts(b)[-1]["events_delivered"] == 0 for b in bids)
    assert "delivery:hermes:test-route:http_5xx" in {i["fingerprint"] for i in r.store.issue_list("open")}


def test_ignored_and_coalesced_are_ok_without_an_issue_when_unwrapped(rig, stub, clock):
    s = stub()
    r = unwrap_rig(rig, s)
    evs = enqueue_batch(r, clock, payloads(2))
    s.respond({"status": 200, "body": b'{"status":"ignored"}'}, {"status": 200, "body": b'{"status":"coalesced"}'})
    r.d.drain_once()
    assert r.batch(evs[0].batch_id)["state"] == "delivered"
    assert not [i for i in r.store.issue_list("open") if i["fingerprint"].endswith(":ignored")]


# ---- size ---------------------------------------------------------------------------------------
def test_max_body_bytes_applies_to_each_event_not_the_document(rig, stub, clock):
    s = stub()
    one = len(json.dumps(payloads(1)[0], separators=(",", ":"), ensure_ascii=False).encode())
    r = unwrap_rig(rig, s, spec=dict(UNWRAP, max_body_bytes=one + 10))
    evs = enqueue_batch(r, clock, payloads(3))          # the document is far over the cap, each event is not
    r.d.drain_once()
    assert r.batch(evs[0].batch_id)["state"] == "delivered" and len(s.requests) == 3
    big = r.store.enqueue("src", "big", DEST, {"note": SENTINEL, "pad": "x" * (one + 50)}, quiet_s=0, max_s=0)
    r.d.drain_once()
    b = r.batch(big.batch_id)
    assert (b["state"], b["last_class"]) == ("dead", "permanent") and f"event {big.event_id}" in b["last_error"]
    assert len(s.requests) == 3                          # nothing sent for the oversized event


# ---- http ---------------------------------------------------------------------------------------
def test_http_unwrap_sends_each_stored_payload_signed(rig, stub, clock, tmp_path):
    s = stub()
    sec = tmp_path / "out.secret"
    sec.write_text("out-secret-1\n")
    os.chmod(sec, 0o600)
    spec = {"type": "http", "url": s.url + "/in", "allow_insecure": True, "secret": {"file": str(sec)},
            "headers": {"X-Env": "test"}, **UNWRAP}
    r = rig({"destinations": {"out": spec}}, destination_factory=build)
    evs = enqueue_batch(r, clock, payloads(2), dest="out", types=["push", "push"], dedups=["d1", "d2"])
    bid = evs[0].batch_id
    s.respond({"status": 204}, {"status": 500})
    r.d.drain_once()
    assert r.batch(bid)["state"] == "retry"
    clock.advance(60)
    r.d.drain_once()
    assert r.batch(bid)["state"] == "delivered"
    inst = r.store.instance_id()
    stored = [p for _, p in r.store.stored_payloads(bid)]
    assert [q["body"] for q in s.requests] == [stored[0], stored[1], stored[0], stored[1]]
    for q, i in zip(s.requests, [0, 1, 0, 1]):
        h = q["headers"]
        assert h["webhook-id"] == f"hs_{inst}_{bid}_{evs[i].event_id}"
        assert verify_standard_webhooks(h, q["body"], "out-secret-1", now=clock())
        assert verify_legacy(h, q["body"], "out-secret-1")
        assert (h["X-GitHub-Event"], h["X-GitHub-Delivery"], h["X-Env"]) == ("push", f"d{i + 1}", "test")


# ---- the shared loop --------------------------------------------------------------------------
def test_unwrap_deliver_without_payloads_is_a_transport_retry_not_a_loss():
    doc = doc_for(2)
    out = unwrap_deliver(doc, "hs_x_7", make_ctx(), lambda m, b, i: Outcome.ok())
    assert (out.kind, out.klass) == ("retry", "transport")
    out = unwrap_deliver(doc, "hs_x_7", replace(make_ctx(), payloads=(b"{}",)), lambda m, b, i: Outcome.ok())
    assert out.klass == "transport"                     # misaligned with event_meta


def test_unwrap_deliver_stops_at_the_first_failure_and_reports_progress():
    doc = doc_for(3)
    seen = []

    def send(meta, body, eid):
        seen.append((meta["id"], body, eid))
        return Outcome.throttle("http 429", status=429, retry_after_s=9) if meta["id"] == 1 else Outcome.ok()
    out = unwrap_deliver(doc, "hs_x_7", replace(make_ctx(), payloads=(b"a", b"b", b"c")), send)
    assert seen == [(0, b"a", "hs_x_7_0"), (1, b"b", "hs_x_7_1")]
    assert (out.klass, out.retry_after_s, out.events_delivered, out.failed_event_id) == ("throttle", 9, 1, 1)
    assert out.reason == "event 1 (2 of 3): http 429"


def test_a_destination_out_of_time_stops_sending(make_config, stub):
    s = stub()
    d = build(make_config(port=s.port, destinations={DEST: dict(UNWRAP)}), DEST)
    doc = doc_for(2)
    ctx = replace(make_ctx(wall=True, ttl=-1), payloads=(b"{}", b"{}"))
    out = d.deliver(doc, encode(doc), "hs_x_7", ctx)
    assert (out.klass, out.events_delivered, out.failed_event_id) == ("transport", 0, 0)
    assert "deadline" in out.reason and s.requests == []


# ---- logs and stored errors carry no bodies -----------------------------------------------------
def test_no_body_in_logs_errors_or_attempt_rows(rig, stub, clock, caplog):
    s = stub()
    r = unwrap_rig(rig, s)
    evs = enqueue_batch(r, clock, payloads(2))
    s.respond({"status": 500, "body": SENTINEL.encode()}, {"status": 400, "body": SENTINEL.encode()})
    r.d.drain_once()
    clock.advance(60)
    r.d.drain_once()
    b = r.batch(evs[0].batch_id)
    assert b["state"] == "dead"
    stored = json.dumps([b, r.store.attempts(evs[0].batch_id), r.store.issue_list()], default=str)
    assert SENTINEL not in stored and "café" not in stored
    assert caplog.records and SENTINEL not in caplog.text       # the suite-wide fixture checks the rendered form


# ---- check --------------------------------------------------------------------------------------
@pytest.fixture
def ucfg(tmp_path, subs_path):
    def make(spec=None, *, window=(0, 0), subs=None):
        write_subs(subs_path, subs if subs is not None else {
            "gh": {"secret": "s", "events": ["pull_request"], "coalesce": {"window_s": 30}}})
        cfg = C.merge(C.DEFAULTS, {
            "spool": {"home": str(tmp_path / "home")},
            "hermes": {"subscriptions_path": str(subs_path)},
            "sources": {"github": {"module": "hookspool.sources.template", "action": "hermes:gh",
                                   "quiet_s": window[0], "max_s": window[1]}},
        })
        if spec is not None:
            cfg["destinations"] = {"hermes:gh": spec}
        return C.check(cfg, load_plugin=None, check_secrets=False)
    return make


def has(msgs, *parts):
    return any(all(p in m for p in parts) for m in msgs)


def test_check_still_refuses_gateway_filter_and_coalesce_without_unwrap(ucfg):
    rep = ucfg()
    assert has(rep.errors, "hermes:gh", "'events'") and has(rep.errors, "hermes:gh", "'coalesce'")
    rep = ucfg({"unwrap": False})
    assert has(rep.errors, "hermes:gh", "'events'")


def test_check_allows_gateway_filter_and_coalesce_with_unwrap(ucfg):
    rep = ucfg(dict(UNWRAP))
    assert rep.ok, rep.errors
    assert not has(rep.warnings, "unwrap")


def test_check_warns_but_passes_when_an_unwrapped_route_coalesces(ucfg):
    rep = ucfg(dict(UNWRAP), window=(30, 120))
    assert rep.ok, rep.errors
    assert has(rep.warnings, "hermes:gh", "unwrap", "burst")


@pytest.mark.parametrize("spec,msg", [
    ({"unwrap": "yes"}, "unwrap: must be true or false"),
    ({"unwrap": True, "event_header": "webhook-id"}, "event_header: must be a plain header name"),
    ({"unwrap": True, "dedup_header": "bad name"}, "dedup_header: must be a plain header name"),
    ({"unwrap": True, "event_header": "X-A", "dedup_header": "x-a"}, "name the same header"),
])
def test_check_validates_the_unwrap_options(ucfg, spec, msg):
    assert has(ucfg(spec).errors, msg)


def test_check_warns_about_unwrap_headers_without_unwrap(ucfg):
    rep = ucfg({"event_header": "X-GitHub-Event"}, subs={"gh": {"secret": "s"}})
    assert rep.ok and has(rep.warnings, "event_header", "only used with unwrap")


def test_check_knows_the_unwrap_keys_for_http(tmp_path):
    cfg = C.merge(C.DEFAULTS, {"destinations": {"out": {
        "type": "http", "url": "https://example.invalid/h", "secret": {"env": "X"}, "unwrap": True,
        "event_header": "X-Kind", "dedup_header": "X-Id", "headers": {"x-kind": "v"}}}})
    rep = C.check(cfg, load_plugin=None, check_secrets=False)
    assert not has(rep.errors, "unknown key") and has(rep.errors, "headers", "also the event_header")
    cfg["destinations"]["s"] = {"type": "script", "argv": ["/bin/true"], "unwrap": True}
    assert has(C.check(cfg, load_plugin=None, check_secrets=False).errors, "destinations.s", "unwrap")


def test_time_is_spent_once_per_attempt_not_per_event(make_config, stub):
    """All the requests of an attempt share its deadline: a slow receiver cannot stretch a batch past it."""
    s = stub()
    s.default = {"status": 200, "body": b'{"status":"accepted"}', "delay": 0.3}
    d = build(make_config(port=s.port, destinations={DEST: dict(UNWRAP)}), DEST)
    doc = doc_for(5)
    ctx = replace(make_ctx(wall=True, ttl=0.7), payloads=tuple(b"{}" for _ in range(5)))
    t0 = time.monotonic()
    out = d.deliver(doc, encode(doc), "hs_x_7", ctx)
    assert out.klass == "transport" and "deadline" in out.reason and time.monotonic() - t0 < 2.0
    assert 1 <= out.events_delivered < 5
