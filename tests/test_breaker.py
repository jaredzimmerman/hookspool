"""Circuit breaker (C2, ARCHITECTURE §4.2): trip on consecutive transport failures on the
destination, rotating probe, poison by comparison (repeated failures of one batch, each after
another batch's success),
re-trip only for probes, probe_until."""
import threading
import time

import pytest

from _dispatchkit import DEST
from conftest import T0
from hookspool import breaker as br
from hookspool.destinations.base import Outcome

P = br.Params(trip_after=3, cooldown_initial_s=30, cooldown_max_s=600)
FAIL = Outcome.transport("connection refused")


# ---- pure functions ---------------------------------------------------------------------------------
def row(**kw):
    d = {"state": "closed", "consecutive_failures": 0, "opened_at": None, "cooldown_s": None, "probe_until": None,
         "last_probe_batch_id": None, "retry_after_until": None}
    d.update(kw)
    return d


def test_gate_decisions():
    assert br.gate(row(), T0, P) == (br.CLAIM, {})
    assert br.gate(row(state="paused"), T0, P) == (br.SKIP, {})
    assert br.gate(row(state="open", opened_at=T0, cooldown_s=30), T0 + 29, P) == (br.SKIP, {})
    d, upd = br.gate(row(state="open", opened_at=T0, cooldown_s=30), T0 + 30, P)
    assert d == br.PROBE and upd == {"state": "half_open", "probe_until": None}
    assert br.gate(row(state="half_open", probe_until=T0 + 10), T0 + 5, P) == (br.SKIP, {})
    assert br.gate(row(state="half_open", probe_until=T0 + 10), T0 + 10, P) == (br.PROBE, {})
    assert br.gate(row(retry_after_until=T0 + 5), T0, P) == (br.SKIP, {})
    assert br.gate(row(retry_after_until=T0 + 5), T0 + 5, P) == (br.CLAIM, {})


def test_transport_failure_trips_and_retrips():
    f, what = br.on_transport_failure(row(consecutive_failures=1), T0, T0, P, "e")
    assert what is None and f["consecutive_failures"] == 2
    f, what = br.on_transport_failure(row(consecutive_failures=2), T0, T0 + 1, P, "e")
    assert what == "tripped" and f["state"] == "open" and f["opened_at"] == T0 + 1 and f["cooldown_s"] == 30
    # an attempt in flight when the breaker opened records nothing else
    f, what = br.on_transport_failure(row(state="open", opened_at=T0 + 1, cooldown_s=30, consecutive_failures=3),
                                      T0, T0 + 2, P, "e")
    assert what is None and f == {"last_error": "e"}
    # a probe (started after opened_at) re-trips with a doubled cooldown, capped
    f, what = br.on_transport_failure(row(state="half_open", opened_at=T0, cooldown_s=30, consecutive_failures=3),
                                      T0 + 40, T0 + 41, P, "e")
    assert what == "retripped" and f["cooldown_s"] == 60 and f["opened_at"] == T0 + 41 and f["state"] == "open"
    f, _ = br.on_transport_failure(row(state="half_open", opened_at=T0, cooldown_s=500), T0 + 1, T0 + 2, P, "e")
    assert f["cooldown_s"] == 600
    f, what = br.on_transport_failure(row(state="paused"), T0, T0, P, "e")
    assert what is None and f == {"last_error": "e"}


def test_success_closes_but_keeps_pause():
    f = br.on_success(row(state="half_open", consecutive_failures=5, cooldown_s=120, opened_at=T0), T0 + 9)
    assert f["state"] == "closed" and f["consecutive_failures"] == 0 and f["cooldown_s"] is None
    assert f["last_ok_at"] == T0 + 9
    assert "state" not in br.on_success(row(state="paused"), T0)


def test_params_from_config():
    assert br.Params.from_config({"dispatch": {"breaker": {"trip_after": 5}}}) == br.Params(5, 30.0, 600.0)


# ---- dispatcher integration -------------------------------------------------------------------------
def fail_n(r, n):
    """Let the head batch fail n times, advancing to each retry's due time."""
    for _ in range(n):
        r.d.drain_once()
        nxt = [b["next_attempt_at"] for b in r.store.list("retry")]
        if nxt:
            r.at(max(r.clock.t, min(nxt)))


def test_trips_on_one_key(rig):
    r = rig()
    r.stub().script(FAIL, FAIL, FAIL)
    a = r.enqueue()
    r.d.drain_once()                       # T0: failure 1, due +15
    assert r.dest()["state"] == "closed" and r.dest()["consecutive_failures"] == 1
    r.at(T0 + 15)
    r.d.drain_once()                       # failure 2, due +30
    r.at(T0 + 45)
    r.d.drain_once()                       # failure 3: trip
    d = r.dest()
    assert d["state"] == "open" and d["opened_at"] == T0 + 45 and d["cooldown_s"] == 30
    b = r.batch(a.batch_id)
    assert b["state"] == "retry" and b["attempts"] == 0 and b["next_attempt_at"] == T0 + 45 + 60
    iss = r.store.issue_get(f"delivery:{DEST}:unreachable")
    assert iss["state"] == "open" and iss["kind"] == "delivery"


def test_open_breaker_claims_nothing_and_batches_wait(rig):
    r = rig({"dispatch": {"breaker": {"trip_after": 1}}})
    s = r.stub().script(FAIL)
    a = r.enqueue("a")
    r.d.drain_once()
    others = [r.enqueue(k, n=i) for i, k in enumerate("bcd")]
    r.at(T0 + 29)
    assert r.d.drain_once() == 0
    assert all(r.batch(x.batch_id)["state"] == "sealed" for x in others)
    assert len(s.calls) == 1 and len(r.store.attempts(others[0].batch_id)) == 0
    assert r.batch(a.batch_id)["state"] == "retry"


def test_inflight_attempts_do_not_retrip(rig):
    r = rig()
    s = r.stub(max_inflight=4).script(FAIL, FAIL, FAIL, FAIL)
    s.hold = threading.Event()
    bs = [r.enqueue(k) for k in "abcd"]
    assert r.d.tick() == 4
    for _ in range(4):
        assert s.started.acquire(timeout=5)
    s.hold.set()
    r.d.drain_once()
    d = r.dest()
    assert d["state"] == "open" and d["cooldown_s"] == 30 and d["opened_at"] == T0
    assert d["consecutive_failures"] == 3
    assert all(r.batch(b.batch_id)["state"] == "retry" for b in bs)


def _trip_with_two_batches(r, s):
    """A and B fail in flight (trip); the probe at T0+30 (A) fails; the breaker reopens at 60 s."""
    s.hold = threading.Event()
    A = r.enqueue("A")
    B = r.enqueue("B")
    assert r.d.tick() == 2
    s.started.acquire(timeout=5)
    s.started.acquire(timeout=5)
    s.hold.set()
    r.d.drain_once()
    s.hold = None
    assert r.dest()["state"] == "open"
    r.at(T0 + 30)                               # cooldown over: half_open, probe = oldest due = A
    r.d.drain_once()
    pa = r.store.attempts(A.batch_id)[-1]
    assert pa["probe"] == 1 and pa["counted"] == 0 and pa["klass"] == "transport"
    d = r.dest()
    assert d["state"] == "open" and d["cooldown_s"] == 60 and d["last_probe_batch_id"] == A.batch_id
    return A, B


def test_probe_rotates_and_recovery_delivers_the_suspect(rig):
    """An ordinary outage: the last failed probe's batch is only a suspect. When the next
    (rotated) probe succeeds the breaker closes and the suspect drains with everything else."""
    r = rig({"dispatch": {"breaker": {"trip_after": 1}}})
    s = r.stub().script(FAIL, FAIL, FAIL)       # A, B (in flight at trip), probe A; then recovered
    A, B = _trip_with_two_batches(r, s)
    r.at(T0 + 30 + 60)                          # next probe must not be A again
    r.d.drain_once()
    pb = [x for x in r.store.attempts(B.batch_id) if x["probe"]]
    assert len(pb) == 1 and pb[0]["outcome"] == "ok"
    assert r.batch(B.batch_id)["state"] == "delivered"
    a = r.batch(A.batch_id)
    assert a["state"] == "delivered" and a["last_class"] is None
    assert [x["outcome"] for x in r.store.attempts(A.batch_id)] == ["retry", "retry", "ok"]
    d = r.dest()
    assert d["state"] == "closed" and d["consecutive_failures"] == 0 and d["last_ok_at"] == T0 + 90
    assert r.store.issue_get(f"delivery:{DEST}:poison") is None
    assert r.store.issue_get(f"delivery:{DEST}:unreachable")["state"] == "resolved"
    assert len(s.calls) == 5


def _fail_a(doc, ctx):
    return FAIL if doc["key"] == "A" else Outcome.ok()


def _strike(r, n):
    """Another batch is accepted, then A (due again) fails: one strike."""
    r.at(r.clock.t + 1)
    other = r.enqueue(f"other{n}")
    r.d.drain_once()
    assert r.batch(other.batch_id)["state"] == "delivered", (r.dest(), r.store.candidates(r.clock.t))
    r.at(max(r.clock.t, r.batch(A_ID[0])["next_attempt_at"]))
    r.d.drain_once()


A_ID = [0]


def test_true_poison_dies_after_repeated_failures_with_successes_between(rig):
    """A fails as a probe, B's probe succeeds (closed), and A keeps failing with transport while
    other batches are accepted between its attempts: after POISON_STRIKES such strikes A is poison
    and dead with an issue; the destination keeps draining and the breaker stays closed."""
    from hookspool.dispatch import POISON_STRIKES
    r = rig({"dispatch": {"breaker": {"trip_after": 2}}})
    s = r.stub().script(FAIL, FAIL, FAIL, Outcome.ok(), *[_fail_a] * 40)   # A, B, probe A, probe B ok, then by key
    A, B = _trip_with_two_batches(r, s)
    A_ID[0] = A.batch_id
    r.at(T0 + 30 + 60)
    r.d.drain_once()
    assert r.batch(B.batch_id)["state"] == "delivered"
    a = r.batch(A.batch_id)
    assert a["state"] == "retry" and a["last_class"] == "transport"    # one failure after a success is a flap
    for n in range(POISON_STRIKES - 1):
        assert r.batch(A.batch_id)["state"] == "retry", n
        _strike(r, n)
    a = r.batch(A.batch_id)
    assert a["state"] == "dead" and a["last_class"] == "poison" and a["attempts"] == 0
    last = r.store.attempts(A.batch_id)[-1]
    assert last["outcome"] == "dead" and last["klass"] == "poison" and last["probe"] == 0 and last["counted"] == 0
    d = r.dest()
    assert d["state"] == "closed" and d["consecutive_failures"] <= 1    # the failure is the batch's
    iss = r.store.issue_get(f"delivery:{DEST}:poison")
    assert iss["state"] == "open" and str(A.batch_id) in iss["last_error"]
    assert r.store.issue_get(f"delivery:{DEST}:unreachable")["state"] == "resolved"
    # the destination drains: B's successor and a new key deliver at once
    B2 = r.enqueue("B")
    C = r.enqueue("C")
    r.d.drain_once()
    assert r.batch(B2.batch_id)["state"] == "delivered" and r.batch(C.batch_id)["state"] == "delivered"
    # replaying the poison batch resolves its issue
    assert r.store.replay(A.batch_id)
    assert r.store.issue_get(f"delivery:{DEST}:poison")["state"] == "resolved"


def test_poison_on_a_healthy_destination_is_found_without_a_breaker_trip(rig):
    """T9: a batch that crashes the receiver every time, on a destination that keeps accepting
    the other batches, never trips the breaker, so it never became a probe 'suspect' and blocked
    its group for ever with no issue. Repeated strikes find it."""
    from hookspool.dispatch import POISON_STRIKES
    r = rig()
    r.stub().script(*[_fail_a] * 60)
    A = r.enqueue("A")
    A_ID[0] = A.batch_id
    r.d.drain_once()
    for n in range(POISON_STRIKES):
        assert r.batch(A.batch_id)["state"] == "retry", n
        assert r.dest()["state"] == "closed"
        _strike(r, n)
    a = r.batch(A.batch_id)
    assert a["state"] == "dead" and a["last_class"] == "poison"
    assert len(r.store.attempts(A.batch_id)) == POISON_STRIKES + 1
    assert r.store.issue_get(f"delivery:{DEST}:poison")["state"] == "open"


def test_a_flaky_destination_does_not_kill_a_batch_that_fails_now_and_then(rig):
    """The soak finding: under a receiver that fails at random, one failed probe followed by one
    more failure after a success used to be enough to call a healthy batch poison. Three failures
    in a row, with other batches accepted between them, are still a flaky receiver."""
    r = rig()
    fails = {"A": 0}

    def a_fails_three_times(doc, ctx):
        if doc["key"] == "A" and fails["A"] < 3:
            fails["A"] += 1
            return FAIL
        return Outcome.ok()
    r.stub().script(*[a_fails_three_times] * 40)
    A = r.enqueue("A")
    A_ID[0] = A.batch_id
    r.d.drain_once()
    for n in range(2):
        _strike(r, n)
    assert r.batch(A.batch_id)["state"] == "retry" and fails["A"] == 3
    r.at(r.clock.t + 1)
    o = r.enqueue("o")
    r.d.drain_once()
    r.at(r.batch(A.batch_id)["next_attempt_at"])
    r.d.drain_once()
    assert r.batch(o.batch_id)["state"] == "delivered" and r.batch(A.batch_id)["state"] == "delivered"
    assert r.store.issue_get(f"delivery:{DEST}:poison") is None


def test_suspect_is_not_poison_without_a_later_success(rig):
    """A failed probe followed by an operator resume (closed, but no success since the probe)
    and then another transport failure is an outage, not poison: the breaker trips, A stays due."""
    r = rig({"dispatch": {"breaker": {"trip_after": 1}}})
    s = r.stub().script(Outcome.ok(), FAIL, FAIL, FAIL, FAIL, FAIL)   # Z ok; A, B, probe A; A and B after resume
    Z = r.enqueue("Z")
    r.d.drain_once()
    assert r.batch(Z.batch_id)["state"] == "delivered" and r.dest()["last_ok_at"] == T0
    A, B = _trip_with_two_batches(r, s)
    r.store.destination_set(DEST, state="paused", paused_at=T0 + 31, paused_by="op")
    r.store.destination_set(DEST, state="closed", consecutive_failures=0, opened_at=None, cooldown_s=None,
                            paused_at=None, paused_by=None)
    r.at(T0 + 30 + 60)
    r.d.drain_once()                            # A is due and the gate is closed: A runs and fails again
    a = r.batch(A.batch_id)
    assert a["state"] == "retry" and a["last_class"] == "transport"
    assert r.batch(B.batch_id)["state"] == "retry"
    assert r.dest()["state"] == "open"
    assert r.store.issue_get(f"delivery:{DEST}:poison") is None


def test_suspect_failing_while_the_breaker_is_open_again_is_not_poison(rig):
    """B's probe succeeds, then the destination goes down again (C trips it) while the suspect A
    is in flight: A's failure is recorded as an outage, not poison."""
    r = rig({"dispatch": {"breaker": {"trip_after": 1}}})
    gate = threading.Event()

    def fail_a_slowly(doc, ctx):
        if doc["key"] == "A":
            gate.wait(5)
        return FAIL
    s = r.stub(max_inflight=4).script(FAIL, FAIL, FAIL, Outcome.ok(), fail_a_slowly, fail_a_slowly)
    A, B = _trip_with_two_batches(r, s)
    C = r.enqueue("C")
    r.at(T0 + 30 + 60)
    assert r.d.tick() == 1                      # the probe (B) only
    until = time.monotonic() + 5
    while r.dest()["state"] != "open" and time.monotonic() < until:   # B ok -> closed; A and C run; C trips
        r.d.tick()
        time.sleep(0.005)
    assert r.batch(B.batch_id)["state"] == "delivered" and r.batch(C.batch_id)["state"] == "retry"
    assert r.batch(A.batch_id)["state"] == "leased"
    gate.set()
    r.d.drain_once()
    a = r.batch(A.batch_id)
    assert a["state"] == "retry" and a["last_class"] == "transport"
    assert r.dest()["state"] == "open"
    assert r.store.issue_get(f"delivery:{DEST}:poison") is None


def test_plain_failure_then_success_then_failure_is_not_poison(rig):
    """An ordinary transport failure followed by another batch's success and a second failure
    is just a destination that flaps: one strike is far from enough."""
    r = rig()
    s = r.stub().script(FAIL, Outcome.ok(), FAIL)
    A = r.enqueue("A")
    r.d.drain_once()
    r.at(T0 + 5)
    B = r.enqueue("B")
    r.d.drain_once()
    assert r.batch(B.batch_id)["state"] == "delivered" and r.dest()["last_ok_at"] == T0 + 5
    r.at(r.batch(A.batch_id)["next_attempt_at"])
    r.d.drain_once()
    a = r.batch(A.batch_id)
    assert a["state"] == "retry" and a["last_class"] == "transport" and len(r.store.attempts(A.batch_id)) == 2
    assert r.dest()["state"] == "closed" and r.dest()["consecutive_failures"] == 1
    assert r.store.issue_get(f"delivery:{DEST}:poison") is None
    assert len(s.calls) == 3


def test_single_batch_destination_stays_open_and_never_kills_it(rig):
    r = rig({"dispatch": {"max_attempts": 2}})
    s = r.stub().script(*[FAIL] * 40)
    a = r.enqueue()
    cooldowns = []
    for _ in range(14):
        r.d.drain_once()
        d, b = r.dest(), r.batch(a.batch_id)
        if d["state"] == "open":
            cooldowns.append(d["cooldown_s"])
            r.at(max(d["opened_at"] + d["cooldown_s"], b["next_attempt_at"]))
        else:
            r.at(b["next_attempt_at"])
    b = r.batch(a.batch_id)
    assert b["state"] == "retry" and b["attempts"] == 0
    assert cooldowns[:7] == [30, 60, 120, 240, 480, 600, 600][:len(cooldowns[:7])]
    assert max(cooldowns) == 600 and len(cooldowns) >= 7
    probes = [x for x in r.store.attempts(a.batch_id) if x["probe"]]
    assert probes and all(x["counted"] == 0 for x in probes)
    iss = r.store.issue_get(f"delivery:{DEST}:unreachable")
    assert str(a.batch_id) in iss["last_error"] and "poison" in iss["last_error"]
    assert len(s.calls) == 14


def test_probe_until_blocks_other_claims(rig):
    r = rig({"dispatch": {"breaker": {"trip_after": 1}}})
    s = r.stub(max_inflight=4).script(FAIL)
    A = r.enqueue("A")
    r.d.drain_once()
    B = r.enqueue("B")
    C = r.enqueue("C")
    s.hold = threading.Event()
    r.at(T0 + 30)
    assert r.d.tick() == 1                     # exactly one probe
    assert s.started.acquire(timeout=5)
    d = r.dest()
    probe_id = d["last_probe_batch_id"]
    assert d["state"] == "half_open" and d["probe_until"] == r.batch(probe_id)["lease_until"]
    assert probe_id == A.batch_id              # oldest due batch; no previous probe to rotate from
    r.at(T0 + 40)
    assert r.d.tick() == 0                     # probe_until in the future: nothing else
    s.hold.set()
    r.d.drain_once()
    assert all(r.batch(x.batch_id)["state"] == "delivered" for x in (A, B, C))
    assert r.dest()["state"] == "closed" and r.dest()["probe_until"] is None


def test_throttle_and_batch_outcomes_are_breaker_neutral(rig):
    r = rig({"dispatch": {"breaker": {"trip_after": 2}}})
    r.stub().script(FAIL, Outcome.throttle("429", status=429), Outcome.batch("exit 1", status=1),
                    Outcome.config("401", status=401))
    a = r.enqueue()
    fail_n(r, 4)
    d = r.dest()
    assert d["state"] == "closed" and d["consecutive_failures"] == 1
    assert r.batch(a.batch_id)["attempts"] == 1


def test_success_on_any_attempt_closes(rig):
    r = rig({"dispatch": {"breaker": {"trip_after": 1}}})
    s = r.stub(max_inflight=2).script(FAIL, Outcome.ok())
    s.hold = threading.Event()
    r.enqueue("a")
    b = r.enqueue("b")
    assert r.d.tick() == 2
    s.started.acquire(timeout=5)
    s.started.acquire(timeout=5)
    s.hold.set()
    r.d.drain_once()
    # failure trips first, then the in-flight success closes it
    assert r.dest()["state"] == "closed"
    assert r.batch(b.batch_id)["state"] == "delivered"


def test_paused_destination_ignores_inflight_failures(rig):
    r = rig()
    s = r.stub().script(FAIL)
    s.hold = threading.Event()
    a = r.enqueue()
    assert r.d.tick() == 1 and s.started.acquire(timeout=5)
    r.store.destination_set(DEST, state="paused", paused_at=T0, paused_by="op")
    s.hold.set()
    r.d.drain_once()
    assert r.dest()["state"] == "paused" and r.dest()["consecutive_failures"] == 0
    assert r.batch(a.batch_id)["state"] == "retry"


@pytest.mark.parametrize("trip_after", [1, 3])
def test_recovery_drains_fifo_after_outage(rig, trip_after):
    r = rig({"dispatch": {"breaker": {"trip_after": trip_after}}})
    s = r.stub().script(*[FAIL] * trip_after)
    first = [r.enqueue("K", n=i, max_events=1) for i in range(3)]
    fail_n(r, trip_after)
    assert r.dest()["state"] == "open"
    r.clock.advance(3600)
    r.d.drain_once()
    assert [c["doc"]["batch_id"] for c in s.calls[trip_after:]] == [x.batch_id for x in first]
    assert all(r.batch(x.batch_id)["state"] == "delivered" for x in first)
