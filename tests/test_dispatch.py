"""Dispatcher (C2): poll order, FIFO, outcome classes, backoff, leases, deadline, clock,
config reload, retention, and the single-instance lock."""
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from _dispatchkit import DEST, StubDestination
from conftest import SENTINEL, T0, child_env
from hookspool import store as store_mod
from hookspool.destinations import build
from hookspool.destinations.base import DeliveryContext, Outcome, Unavailable, encode_body
from hookspool.dispatch import DEADLINE_MARGIN_S, Dispatcher, backoff_s, reason_class
from hookspool.store import LockHeld


# ---- backoff ---------------------------------------------------------------------------------
def test_backoff_schedule():
    assert [backoff_s(n) for n in range(1, 8)] == [15, 30, 60, 120, 240, 300, 300]
    assert backoff_s(0) == 15
    assert backoff_s(64) == 300 and backoff_s(10_000) == 300
    assert backoff_s(1, jitter=0.25) == pytest.approx(18.75)
    assert backoff_s(7, jitter=0.25) == pytest.approx(375)
    assert backoff_s(2, base=10, cap=15) == 15


def test_jitter_default_is_bounded(rig):
    r = rig(start=False, jitter=None)
    vals = [r.d._backoff(1) for _ in range(200)]
    assert all(15 <= v <= 18.75 for v in vals) and len(set(vals)) > 1


# ---- delivery and the document ------------------------------------------------------------------
def test_delivers_sealed_batch_with_document_and_per_batch_id(rig):
    r = rig()
    s = r.stub()
    a = r.enqueue(n=1, event_type="push")
    r.enqueue(n=2, event_type="push")                    # same poll: same batch
    assert r.d.drain_once() == 1
    assert len(s.calls) == 1
    c = s.calls[0]
    doc = c["doc"]
    assert doc["count"] == 2 and [e["n"] for e in doc["events"]] == [1, 2]
    assert c["body"] == encode_body(doc)
    inst = r.store.instance_id()
    assert c["delivery_id"] == f"hs_{inst}_{a.batch_id}" == doc["delivery_id"]
    assert doc["attempt"] == 1 == c["ctx"].attempt
    b = r.batch(a.batch_id)
    assert b["state"] == "delivered" and b["attempts"] == 1 and b["lease_owner"] is None
    att = r.store.attempts(a.batch_id)
    assert [(x["outcome"], x["counted"], x["probe"]) for x in att] == [("ok", 1, 0)]


def test_per_attempt_delivery_id(rig):
    r = rig()
    s = r.stub(idempotency="per-attempt").script(Outcome.transport("refused"), Outcome.ok())
    a = r.enqueue()
    r.d.drain_once()
    r.clock.advance(15)
    r.d.drain_once()
    ids = [c["delivery_id"] for c in s.calls]
    atts = r.store.attempts(a.batch_id)
    inst = r.store.instance_id()
    assert ids == [f"hs_{inst}_{a.batch_id}_{x['id']}" for x in atts] and len(set(ids)) == 2
    assert [c["doc"]["attempt"] for c in s.calls] == [1, 2]
    assert s.calls[0]["doc"]["content_hash"] == s.calls[1]["doc"]["content_hash"]


def test_seal_happens_before_the_gate(rig):
    r = rig()
    r.stub()
    r.store.destination_set(DEST, state="paused", paused_at=T0, paused_by="test")
    a = r.enqueue(quiet_s=30, max_s=100)
    r.d.tick()
    assert r.batch(a.batch_id)["state"] == "open"
    r.clock.advance(30)
    r.d.tick()
    b = r.batch(a.batch_id)
    assert b["state"] == "sealed" and b["content_hash"] and b["sealed_at"] == T0 + 30
    assert r.store.attempts(a.batch_id) == []
    nxt = r.enqueue(n=9, quiet_s=30, max_s=100)          # a new batch opens behind the sealed one
    assert nxt.batch_id != a.batch_id


def test_pause_and_resume(rig):
    r = rig()
    s = r.stub()
    r.store.destination_set(DEST, state="paused", paused_at=T0, paused_by="test")
    a = r.enqueue()
    assert r.d.drain_once() == 0 and s.calls == []
    r.clock.advance(3600)
    assert r.d.drain_once() == 0
    r.store.destination_set(DEST, state="closed", consecutive_failures=0, opened_at=None, cooldown_s=None,
                            probe_until=None, paused_at=None, paused_by=None)
    assert r.d.drain_once() == 1
    assert r.batch(a.batch_id)["state"] == "delivered"


def test_fifo_per_key_parallel_across_keys(rig):
    r = rig()
    s = r.stub(max_inflight=4)
    s.hold = threading.Event()
    a1 = r.enqueue("A", n=1, max_events=1)
    a2 = r.enqueue("A", n=2, max_events=1)
    b1 = r.enqueue("B", n=3, max_events=1)
    assert r.d.tick() == 2                                # heads only: A1 and B1, never A2
    assert {r.batch(a1.batch_id)["state"], r.batch(b1.batch_id)["state"]} == {"leased"}
    assert r.batch(a2.batch_id)["state"] == "sealed"
    for _ in range(2):
        assert s.started.acquire(timeout=5)
    assert r.d.tick() == 0                                # A's head is leased
    s.hold.set()
    r.d.drain_once()
    order = [c["doc"]["batch_id"] for c in s.calls]
    assert order.index(a1.batch_id) < order.index(a2.batch_id)
    assert all(r.batch(x.batch_id)["state"] == "delivered" for x in (a1, a2, b1))


def test_global_parallel_and_max_inflight_caps(rig):
    r = rig({"dispatch": {"parallel": 3}})
    s = r.stub(max_inflight=2)
    s.hold = threading.Event()
    other = r.stub("hermes:other", max_inflight=5)
    other.hold = s.hold
    for k in "abcd":
        r.enqueue(k)
    for k in "wxyz":
        r.enqueue(k, dest="hermes:other")
    assert r.d.tick() == 3
    assert r.d._inflight[DEST] == 2 and r.d._inflight["hermes:other"] == 1
    s.hold.set()
    r.d.drain_once()
    assert len(s.calls) == 4 and len(other.calls) == 4


# ---- outcome classes -------------------------------------------------------------------------------
@pytest.mark.parametrize("out, klass, counted, delay", [
    (Outcome.transport("refused"), "transport", 0, 15),
    (Outcome.throttle("429", status=429), "throttle", 0, 15),
    (Outcome.batch("exit 1", status=1), "batch", 1, 15),
    (Outcome.config("unknown route", status=404), "config", 0, 300),
])
def test_retry_classes_counted_and_scheduled(rig, out, klass, counted, delay):
    r = rig()
    r.stub().script(out)
    a = r.enqueue()
    r.d.drain_once()
    b = r.batch(a.batch_id)
    assert b["state"] == "retry" and b["last_class"] == klass
    assert b["attempts"] == counted
    assert b["next_attempt_at"] == pytest.approx(T0 + delay)
    att = r.store.attempts(a.batch_id)[-1]
    assert (att["outcome"], att["klass"], att["counted"]) == ("retry", klass, counted)


def test_retry_after_overrides_backoff_and_gates_the_destination(rig):
    r = rig()
    s = r.stub().script(Outcome.throttle("503", status=503, retry_after_s=120))
    a = r.enqueue("a")
    r.d.drain_once()
    assert r.batch(a.batch_id)["next_attempt_at"] == T0 + 120
    assert r.dest()["retry_after_until"] == T0 + 120
    b = r.enqueue("b")                                    # another key, same destination
    r.clock.advance(60)
    assert r.d.drain_once() == 0 and r.batch(b.batch_id)["state"] == "sealed"
    r.clock.advance(60)
    r.d.drain_once()
    assert r.batch(a.batch_id)["state"] == r.batch(b.batch_id)["state"] == "delivered"
    assert r.dest()["consecutive_failures"] == 0 and len(s.calls) == 3


def test_retry_after_is_capped_at_one_hour(rig):
    r = rig()
    r.stub().script(Outcome.throttle("429", status=429, retry_after_s=999999))
    a = r.enqueue()
    r.d.drain_once()
    assert r.batch(a.batch_id)["next_attempt_at"] == T0 + 3600


def test_transport_failures_never_count_toward_max_attempts(rig):
    r = rig({"dispatch": {"max_attempts": 2, "breaker": {"trip_after": 1000}}})
    r.stub().script(*[Outcome.transport("refused")] * 20)
    a = r.enqueue()
    for _ in range(20):
        r.d.drain_once()
        r.clock.advance(400)
    b = r.batch(a.batch_id)
    assert b["state"] == "retry" and b["attempts"] == 0
    assert len(r.store.attempts(a.batch_id)) == 20


def test_batch_class_counts_until_max_attempts_then_dead(rig):
    r = rig({"dispatch": {"max_attempts": 3}})
    r.stub().script(*[Outcome.batch("exit 1", status=1)] * 3)
    a = r.enqueue()
    for delay in (15, 30):
        r.d.drain_once()
        b = r.batch(a.batch_id)
        assert b["state"] == "retry" and b["next_attempt_at"] == pytest.approx(r.clock.t + delay)
        r.clock.advance(delay)
    r.d.drain_once()
    b = r.batch(a.batch_id)
    assert b["state"] == "dead" and b["last_class"] == "batch" and b["attempts"] == 3
    assert r.store.issue_get(f"delivery:{DEST}:exhausted")["state"] == "open"


def test_permanent_is_dead_and_the_group_moves_on(rig):
    r = rig()
    s = r.stub().script(Outcome.permanent("413", status=413))
    a1 = r.enqueue("A", n=1, max_events=1)
    a2 = r.enqueue("A", n=2, max_events=1)
    r.d.drain_once()
    assert r.batch(a1.batch_id)["state"] == "dead" and r.batch(a1.batch_id)["last_class"] == "permanent"
    assert r.batch(a2.batch_id)["state"] == "delivered"
    iss = r.store.issue_get(f"delivery:{DEST}:permanent")
    assert iss["state"] == "open" and f"hookspool replay {a1.batch_id}" in iss["last_error"]
    assert len(s.calls) == 2


def test_ok_with_emits_enqueues_them_in_the_same_mark(rig):
    r = rig()
    emit = {"source": "src", "key": "follow", "action": "hermes:other", "payload": {"x": 1}}
    r.stub().script(Outcome.ok("exit 0", emits=[emit]))
    r.stub("hermes:other")
    a = r.enqueue()
    r.d.drain_once()
    assert r.batch(a.batch_id)["state"] == "delivered"
    follow = r.store.list(destination="hermes:other")
    assert len(follow) == 1 and follow[0]["state"] == "delivered"


def test_bad_emit_is_a_counted_retry_then_permanent(rig):
    r = rig()
    bad = {"source": "src", "key": "k", "payload": "not-an-object", "action": "hermes:other"}
    r.stub().script(Outcome.ok(emits=[bad]), Outcome.ok(emits=[bad]))
    a = r.enqueue()
    r.d.drain_once()
    b = r.batch(a.batch_id)
    assert b["state"] == "retry" and b["last_class"] == "batch" and b["attempts"] == 1
    assert b["last_error"].startswith("bad emit")
    r.clock.advance(15)
    r.d.drain_once()
    b = r.batch(a.batch_id)
    assert b["state"] == "dead" and b["last_class"] == "permanent"
    assert r.store.list(destination="hermes:other") == []


def test_destination_exception_is_a_transport_failure(rig):
    r = rig()

    def boom(doc, ctx):
        raise RuntimeError(SENTINEL)
    r.stub().script(boom)
    a = r.enqueue()
    r.d.drain_once()
    b = r.batch(a.batch_id)
    assert b["state"] == "retry" and b["last_class"] == "transport" and b["attempts"] == 0
    assert "RuntimeError" in b["last_error"] and SENTINEL not in b["last_error"]
    assert r.dest()["consecutive_failures"] == 1


def test_empty_batch_is_delivered_without_calling_the_destination(rig):
    r = rig()
    s = r.stub()
    r.store.destination_set(DEST, state="paused")
    a = r.enqueue()
    r.d.tick()
    ev = r.store.events(a.batch_id)[0]["id"]
    r.store.exclude(a.batch_id, [ev])
    r.store.destination_set(DEST, state="closed")
    r.d.drain_once()
    assert s.calls == [] and r.batch(a.batch_id)["state"] == "delivered"


def test_document_over_max_body_bytes_is_permanent_without_a_request(rig):
    r = rig()
    s = r.stub(max_body_bytes=200)
    a = r.enqueue(n=1)
    r.d.drain_once()
    b = r.batch(a.batch_id)
    assert s.calls == [] and b["state"] == "dead" and b["last_class"] == "permanent"
    assert "max_body_bytes" in b["last_error"]


def test_unbuildable_destinations_are_config_retries(rig):
    r = rig({"destinations": {"x": {"module": "no_such_module_hs:Cls"}}},
            destination_factory=build)
    a = r.enqueue("a", dest="x")
    b = r.enqueue("b", dest="not-a-destination")
    r.d.drain_once()
    for res in (a, b):
        row = r.batch(res.batch_id)
        assert row["state"] == "retry" and row["last_class"] == "config" and row["attempts"] == 0
    assert isinstance(r.d.destination("x"), Unavailable)
    assert r.store.issue_get("config:delivery:x")["state"] == "open"
    assert isinstance(build(r.cfg, "quarantine"), Unavailable)


def test_reason_classes():
    assert reason_class(Outcome.transport("refused"), "hermes") == "unreachable"
    assert reason_class(Outcome.transport("http 502", status=502), "hermes") == "http_5xx"
    assert reason_class(Outcome.transport("timeout after 30s"), "http") == "timeout"
    assert reason_class(Outcome.transport("deadline"), "http") == "timeout"
    assert reason_class(Outcome.batch("exit 3", status=3), "script") == "script_exit"


def test_outcome_validation():
    with pytest.raises(ValueError):
        Outcome("retry", "permanent")
    with pytest.raises(ValueError):
        Outcome("dead", "transport")
    with pytest.raises(ValueError):
        Outcome("ok", "batch")
    with pytest.raises(ValueError):
        Outcome("retry", "batch", emits=[{}])


# ---- leases, deadline, fencing ----------------------------------------------------------------------
def test_lease_and_deadline(rig):
    r = rig({"dispatch": {"lease_grace_s": 60}})
    s = r.stub(timeout_s=30)
    seen = {}

    def check(doc, ctx: DeliveryContext):
        seen["b"] = r.store.batch(ctx.batch_id)
        seen["ctx"] = ctx
        return Outcome.ok()
    s.script(check)
    r.enqueue()
    r.d.drain_once()
    ctx = seen["ctx"]
    assert seen["b"]["state"] == "leased" and seen["b"]["lease_until"] == T0 + 90 == ctx.lease_until
    assert ctx.deadline == T0 + 90 - DEADLINE_MARGIN_S
    assert ctx.remaining() == pytest.approx(85)
    assert ctx.timeout(30) == 30 and ctx.timeout(500) == pytest.approx(85)
    r.clock.advance(100)
    assert ctx.timeout(30) == 0


def test_worker_deadline_is_a_transport_failure(rig):
    r = rig()
    s = r.stub(timeout_s=30)

    def slow(doc, ctx):
        r.clock.advance(200)                              # the request outlived the deadline
        return Outcome.transport("deadline") if ctx.remaining() <= 0 else Outcome.ok()
    s.script(slow)
    a = r.enqueue()
    r.d.drain_once()
    b = r.batch(a.batch_id)
    assert b["state"] == "retry" and b["last_class"] == "transport" and b["attempts"] == 0


def test_startup_recovers_every_lease_uncounted(rig, clock):
    r = rig(start=False)
    a = r.enqueue()
    r.store.seal_ready(T0)
    assert r.store.claim(a.batch_id, 10_000, "crashed-host:1:1")
    r.store.start_attempt(a.batch_id, "crashed-host:1:1")
    recovered = r.d.start()
    assert recovered == [a.batch_id]
    b = r.batch(a.batch_id)
    assert b["state"] == "retry" and b["attempts"] == 0 and b["next_attempt_at"] == T0
    assert b["last_error"] == "lease expired"
    att = r.store.attempts(a.batch_id)[0]
    assert att["outcome"] == "retry" and att["counted"] == 0 and att["error"] == "lease expired"
    r.d.drain_once()
    assert r.batch(a.batch_id)["state"] == "delivered"


def test_tick_recovers_only_expired_leases(rig):
    r = rig()
    r.stub()
    a = r.enqueue()
    r.store.seal_ready(T0)
    assert r.store.claim(a.batch_id, 100, "other:1:1")
    r.clock.advance(99)
    assert r.d.drain_once() == 0 and r.batch(a.batch_id)["state"] == "leased"
    r.clock.advance(2)
    r.d.tick()
    assert r.batch(a.batch_id)["state"] in ("retry", "leased")
    r.d.drain_once()
    assert r.batch(a.batch_id)["state"] == "delivered"


def test_live_worker_lease_is_never_recovered_even_after_clock_jump(rig):
    r = rig()
    s = r.stub(timeout_s=30)
    s.hold = threading.Event()
    a = r.enqueue()
    assert r.d.tick() == 1 and s.started.acquire(timeout=5)
    r.clock.advance(10_000)                               # far past lease_until
    r.d.tick()
    b = r.batch(a.batch_id)
    assert b["state"] == "leased"
    s.hold.set()
    r.d.drain_once()
    assert r.batch(a.batch_id)["state"] == "delivered"


def test_fenced_mark_discards_exactly(rig):
    r = rig()
    s = r.stub().script(Outcome.ok(emits=[{"source": "src", "key": "e", "action": "hermes:other",
                                             "payload": {"x": 1}}]))
    s.hold = threading.Event()
    a = r.enqueue()
    assert r.d.tick() == 1 and s.started.acquire(timeout=5)
    # another owner took the batch over (recovery + re-claim), so the first worker's lease is gone
    r.store.recover_leases(r.clock.t, all_leases=True)
    assert r.store.claim(a.batch_id, 100, "someone-else:1:1")
    s.hold.set()
    while r.d._work:
        r.d._reap()
        time.sleep(0.01)
    b = r.batch(a.batch_id)
    assert b["state"] == "leased" and b["lease_owner"] == "someone-else:1:1"
    att = r.store.attempts(a.batch_id)[0]
    assert att["outcome"] == "retry"                     # finished by the recovery, not by the late result
    assert r.store.list(destination="hermes:other") == []


def test_fenced_mark_marks_attempt_discarded(rig):
    r = rig()
    s = r.stub()
    s.hold = threading.Event()
    a = r.enqueue()
    assert r.d.tick() == 1 and s.started.acquire(timeout=5)
    w = next(iter(r.d._work.values()))
    # steal the lease without finishing the attempt row
    r.store._write(lambda c: c.execute("UPDATE batches SET lease_owner='thief' WHERE id=?", (a.batch_id,)))
    s.hold.set()
    while r.d._work:
        r.d._reap()
        time.sleep(0.01)
    att = [x for x in r.store.attempts(a.batch_id) if x["id"] == w.attempt.id][0]
    assert att["outcome"] == "discarded" and att["counted"] == 0
    assert r.batch(a.batch_id)["state"] == "leased"


# ---- clock ------------------------------------------------------------------------------------------
def test_clock_backwards_time_stands_still(rig):
    r = rig()
    r.stub()
    a = r.enqueue(quiet_s=60, max_s=600)
    r.at(T0 + 50)
    r.d.tick()
    r.at(T0 - 5000)                                       # NTP step back
    assert r.d.now_eff() == T0 + 50
    r.d.tick()
    assert r.batch(a.batch_id)["state"] == "open"
    r.at(T0 + 59)
    r.d.drain_once()
    assert r.batch(a.batch_id)["state"] == "open"
    r.at(T0 + 61)
    r.d.drain_once()
    b = r.batch(a.batch_id)
    assert b["state"] == "delivered" and b["first_at"] == T0 and b["last_at"] == T0


def test_last_tick_persists_across_restart(rig):
    r = rig()
    r.at(T0 + 1000)
    r.d.tick()
    r.d._heartbeat(r.d.now_eff(), force=True)
    r.d.close()
    r.at(T0)
    d2 = r.dispatcher()
    d2.start()
    try:
        assert d2.now_eff() == T0 + 1000
    finally:
        d2.close()


def test_clock_forward_closes_windows(rig):
    r = rig()
    r.stub()
    a = r.enqueue(quiet_s=600, max_s=6000)
    r.d.tick()
    assert r.batch(a.batch_id)["state"] == "open"
    r.clock.advance(10**6)
    r.d.drain_once()
    assert r.batch(a.batch_id)["state"] == "delivered"


# ---- heartbeat, retention ------------------------------------------------------------------------
def test_heartbeat_cadence(rig):
    r = rig()
    assert float(r.store.meta_get("dispatcher_heartbeat_at")) == T0
    assert int(r.store.meta_get("dispatcher_pid")) == os.getpid()
    r.clock.advance(5)
    r.d.tick()
    assert float(r.store.meta_get("dispatcher_heartbeat_at")) == T0
    r.clock.advance(5)
    r.d.tick()
    assert float(r.store.meta_get("dispatcher_heartbeat_at")) == T0 + 10
    st = r.store.status(T0 + 12)
    assert st["dispatcher"]["alive"] and st["dispatcher"]["pid"] == os.getpid()


def test_retention_runs_hourly(rig, monkeypatch):
    r = rig()
    r.stub()
    a = r.enqueue()
    r.d.drain_once()
    calls = []
    real = r.store.prune
    monkeypatch.setattr(r.store, "prune", lambda now=None, **kw: calls.append(now) or real(now, **kw))
    r.clock.advance(8 * 86400)
    r.d.tick()
    assert r.batch(a.batch_id) is None and len(calls) == 1
    for _ in range(30):
        r.clock.advance(61)
        r.d.tick()
    assert len(calls) == 1
    r.clock.advance(1800)
    r.d.tick()
    assert len(calls) == 2


def test_dead_batches_are_never_pruned(rig):
    r = rig()
    r.stub().script(Outcome.permanent("400", status=400))
    a = r.enqueue()
    r.d.drain_once()
    r.clock.advance(400 * 86400)
    r.d.tick()
    assert r.batch(a.batch_id)["state"] == "dead"


# ---- config reload -------------------------------------------------------------------------------
def _write_cfg(path, text, bump=[0]):
    path.write_text(text)
    bump[0] += 1
    os.utime(path, ns=(1_000_000_000 * (1000 + bump[0]), 1_000_000_000 * (1000 + bump[0])))


def test_config_retry_is_redued_by_a_successful_reload(rig, tmp_path):
    cfgfile = tmp_path / "config.toml"
    _write_cfg(cfgfile, '[alerts]\ndry = true\n')
    r = rig(config_path=str(cfgfile))
    s = r.stub().script(Outcome.config("unknown route", status=404))
    a = r.enqueue()
    r.d.drain_once()
    b = r.batch(a.batch_id)
    assert b["state"] == "retry" and b["next_attempt_at"] == T0 + 300
    assert r.store.issue_get(f"config:delivery:{DEST}")["state"] == "open"
    r.clock.advance(5)
    assert r.d.drain_once() == 0
    _write_cfg(cfgfile, '[alerts]\ndry = true\n[dispatch]\nparallel = 2\n')
    r.d.drain_once()
    assert r.d.config["dispatch"]["parallel"] == 2
    assert r.batch(a.batch_id)["state"] == "delivered" and len(s.calls) == 2
    assert r.store.issue_get(f"config:delivery:{DEST}")["state"] == "resolved"


def test_reload_raising_parallel_grows_the_worker_pool(rig, tmp_path):
    cfgfile = tmp_path / "config.toml"
    _write_cfg(cfgfile, '[alerts]\ndry = true\n[dispatch]\nparallel = 1\n')
    r = rig({"dispatch": {"parallel": 1}}, config_path=str(cfgfile))
    s = r.stub(max_inflight=3)
    s.hold = threading.Event()
    for k in ("A", "B", "C"):
        r.enqueue(k)
    assert r.d.tick() == 1 and s.started.acquire(timeout=5)
    _write_cfg(cfgfile, '[alerts]\ndry = true\n[dispatch]\nparallel = 3\n')
    assert r.d.tick() == 2
    # every claimed (leased) batch has a running worker, none waits in the executor queue
    assert s.started.acquire(timeout=5) and s.started.acquire(timeout=5)
    s.hold.set()
    r.d.drain_once()
    assert len(s.calls) == 3 and all(r.batch(c["doc"]["batch_id"])["state"] == "delivered" for c in s.calls)


TEMPLATE_SOURCE = """
[sources.tpl]
module = "hookspool.sources.template"
scheme = "hmac-sha256-hex"
header = "X-Signature"
key = "repo"
action = "script:one"
secrets.secret = { file = "/nonexistent/secret" }

[destinations."script:one"]
argv = ["/bin/true"]
"""


def test_reload_accepts_template_sources(rig, tmp_path):
    # the template plugin's own keys (scheme, header, key, ...) are known only to the plugin; a reload
    # that validated without loading it refused every such config and silently kept the old one
    cfgfile = tmp_path / "config.toml"
    r = rig(config_path=str(cfgfile))                     # no file yet: the dict is the config
    _write_cfg(cfgfile, '[alerts]\ndry = true\n' + TEMPLATE_SOURCE)
    r.d.tick()
    assert r.d.config["destinations"]["script:one"]["argv"] == ["/bin/true"]
    _write_cfg(cfgfile, '[alerts]\ndry = true\n' + TEMPLATE_SOURCE + '\n[destinations."script:two"]\nargv = ["/bin/false"]\n')
    r.d.tick()
    assert r.d.config["destinations"]["script:two"]["argv"] == ["/bin/false"]
    assert r.store.issue_get("config:file") is None or r.store.issue_get("config:file")["state"] != "open"


def test_invalid_reload_keeps_the_old_config_and_opens_an_issue(rig, tmp_path):
    cfgfile = tmp_path / "config.toml"
    r = rig(config_path=str(cfgfile))                     # no file yet: the dict is the config
    _write_cfg(cfgfile, '[alerts]\ndry = true\n[dispatch]\nmax_attempts = 7\n')
    r.d.tick()
    assert r.d.config["dispatch"]["max_attempts"] == 7
    _write_cfg(cfgfile, '[dispatch]\nmax_attempts = 9\nbogus_key = 1\n')
    r.d.tick()
    assert r.d.config["dispatch"]["max_attempts"] == 7
    assert r.store.issue_get("config:file")["state"] == "open"
    _write_cfg(cfgfile, '[dispatch\n')                     # not TOML
    r.d.tick()
    assert r.d.config["dispatch"]["max_attempts"] == 7
    _write_cfg(cfgfile, '[alerts]\ndry = true\n[dispatch]\nmax_attempts = 9\n')
    r.d.tick()
    assert r.d.config["dispatch"]["max_attempts"] == 9
    assert r.store.issue_get("config:file")["state"] == "resolved"
    os.unlink(cfgfile)
    r.d.tick()
    assert r.d.config["dispatch"]["max_attempts"] == 9


# ---- single instance ---------------------------------------------------------------------------------
def test_second_dispatcher_in_process_fails_the_lock(rig):
    r = rig()
    d2 = r.dispatcher()
    with pytest.raises(LockHeld):
        d2.start()
    d2.close()


def test_two_dispatcher_processes_second_exits_3(tmp_path):
    home = tmp_path / "svc"
    home.mkdir()
    (home / "config.toml").write_text('[alerts]\ndry = true\n[dispatch]\npoll_s = 0.1\n')
    env = child_env()
    env.update(HOOKSPOOL_HOME=str(home), HOOKSPOOL_CONFIG=str(home / "config.toml"),
               HOOKSPOOL_DB=str(home / "spool.db"), HOOKSPOOL_ALERT_DRY="1")
    cmd = [sys.executable, "-m", "hookspool", "dispatch"]
    first = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                c = sqlite3.connect(str(home / "spool.db"))
                row = c.execute("SELECT value FROM meta WHERE key='dispatcher_pid'").fetchone()
                c.close()
                if row and int(row[0]) == first.pid:
                    break
            except sqlite3.Error:
                pass
            assert first.poll() is None, first.stderr.read().decode()
            time.sleep(0.05)
        else:
            pytest.fail("first dispatcher never wrote its heartbeat")
        second = subprocess.run(cmd, env=env, capture_output=True, timeout=20)
        assert second.returncode == 3, second.stderr.decode()
        assert b"another dispatcher" in second.stderr
    finally:
        first.send_signal(signal.SIGTERM)
        try:
            rc = first.wait(timeout=20)
        except subprocess.TimeoutExpired:
            first.kill()
            raise
    assert rc == 0


def test_dispatch_cli_exits_2_on_newer_schema(tmp_path):
    home = tmp_path / "svc"
    home.mkdir()
    db = home / "spool.db"
    c = sqlite3.connect(str(db))
    c.execute(f"PRAGMA user_version={store_mod.SCHEMA_VERSION + 1}")
    c.close()
    env = child_env()
    env.update(HOOKSPOOL_HOME=str(home), HOOKSPOOL_CONFIG=str(home / "none.toml"), HOOKSPOOL_DB=str(db))
    p = subprocess.run([sys.executable, "-m", "hookspool", "dispatch"], env=env, capture_output=True, timeout=20)
    assert p.returncode == 2, p.stderr.decode()


def test_run_forever_stops_on_event(rig):
    r = rig({"dispatch": {"poll_s": 0.01}})
    s = r.stub()
    r.enqueue()
    stop = threading.Event()
    t = threading.Thread(target=r.d.run_forever, args=(stop,))
    t.start()
    deadline = time.monotonic() + 10
    while not s.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    t.join(10)
    assert not t.is_alive() and len(s.calls) == 1


def test_unused_dispatcher_constructs_from_files(tmp_path, monkeypatch):
    (tmp_path / "c.toml").write_text('[alerts]\ndry = true\n')
    monkeypatch.setenv("HOOKSPOOL_DB", str(tmp_path / "x.db"))
    d = Dispatcher(config_path=str(tmp_path / "c.toml"), lock_path=str(tmp_path / "l"))
    try:
        d.start()
        assert d.store.check_schema() == store_mod.SCHEMA_VERSION
        assert isinstance(d.destination("nope"), Unavailable)
        assert not isinstance(d.destination("nope"), StubDestination)
    finally:
        d.close()
