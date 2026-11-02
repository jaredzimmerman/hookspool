"""Per-event settlement (ARCHITECTURE §6.4.2): handled state, filtering, partial retries, terminal clearing."""
from __future__ import annotations


from _dispatchkit import DEST
from hookspool import config as hs_config
from hookspool.destinations.base import Outcome
from hookspool.store import Store


# ---- config ----------------------------------------------------------------------------------
def test_destination_per_event_results_helper(make_config):
    cfg = make_config(destinations={
        "out": {"type": "http", "url": "https://example.invalid/h", "secret": {"env": "X"}}
    })
    assert hs_config.destination_per_event_results(cfg, "out") is False
    cfg = make_config(destinations={
        "out": {"type": "http", "url": "https://example.invalid/h", "secret": {"env": "X"},
                "per_event_results": True}
    })
    assert hs_config.destination_per_event_results(cfg, "out") is True
    assert hs_config.destination_per_event_results(cfg, "unknown") is False
    # hermes: true whenever opt-in set (the type is hermes)
    cfg = make_config(destinations={DEST: {"unwrap": True, "per_event_results": True}})
    assert hs_config.destination_per_event_results(cfg, DEST) is True
    cfg = make_config(destinations={DEST: {"per_event_results": True}})
    assert hs_config.destination_per_event_results(cfg, DEST) is True


# ---- store ----------------------------------------------------------------------------------
def test_handled_state_add_clear(tmp_path, make_config):
    cfg = make_config()
    p = str(tmp_path / "s.db")
    Store(p, cfg).close()
    s = Store(p, cfg)
    s.ensure_schema()
    bid = s.enqueue("src", "k", "d", {"n": 0}, quiet_s=0, max_s=0).batch_id
    assert s.handled_event_ids(bid) == set()
    s.mark_handled(bid, [10, 20])
    assert s.handled_event_ids(bid) == {10, 20}
    s.mark_handled(bid, [20, 30])          # idempotent + append
    assert s.handled_event_ids(bid) == {10, 20, 30}
    s.clear_handled(bid)
    assert s.handled_event_ids(bid) == set()
    s.close()


def test_terminal_states_clear_handled(rig, clock):
    r = rig(start=False)
    e1 = r.enqueue(dest=DEST, quiet_s=0, max_s=0)
    bid = e1.batch_id
    r.store.seal_ready(clock.t)
    assert r.store.claim(bid, 60, "owner") is True
    r.store.mark_handled(bid, [1])
    r.store.mark_delivered(bid, "owner")
    assert r.store.handled_event_ids(bid) == set()
    e2 = r.enqueue(dest=DEST, quiet_s=0, max_s=0)
    bid2 = e2.batch_id
    r.store.seal_ready(clock.t)
    assert r.store.claim(bid2, 60, "owner") is True
    r.store.mark_handled(bid2, [1])
    r.store.mark_dead(bid2, "owner", "permanent", "permanent", counted=False)
    assert r.store.handled_event_ids(bid2) == set()
    e3 = r.enqueue(dest=DEST, quiet_s=0, max_s=0)
    bid3 = e3.batch_id
    r.store.seal_ready(clock.t)
    r.store.mark_handled(bid3, [1])
    assert r.store.drop(bid3) is True
    assert r.store.handled_event_ids(bid3) == set()


# ---- dispatch: partial → retry, full → delivered -------------------------------------------
def test_per_event_results_partial_sets_retry(rig, clock):
    r = rig({"destinations": {DEST: {"unwrap": True, "per_event_results": True}}}, start=True)
    evs = [r.enqueue(dest=DEST, quiet_s=0, max_s=0) for i in range(3)]
    assert len({x.batch_id for x in evs}) == 1
    bid = evs[0].batch_id
    stub = r.stub()
    stub.per_event_results = True
    stub.unwrap = True
    stub.script(Outcome.ok("ok", handled=[1]), Outcome.transport("timeout"))
    assert r.d.drain_once() >= 1
    b = r.batch(bid)
    assert b["state"] == "retry", b["last_error"]
    assert r.store.handled_event_ids(bid) == {1}


def test_per_event_results_all_handled_delivers(rig, clock):
    r = rig({"destinations": {DEST: {"unwrap": True, "per_event_results": True}}}, start=True)
    evs = [r.enqueue(dest=DEST, quiet_s=0, max_s=0) for i in range(3)]
    bid = evs[0].batch_id
    stub = r.stub()
    stub.per_event_results = True
    stub.unwrap = True
    stub.script(Outcome.ok("ok", handled=[1, 2, 3]))
    assert r.d.drain_once() == 1
    assert r.batch(bid)["state"] == "delivered"
    assert r.store.handled_event_ids(bid) == set()


def test_per_event_results_missing_handled_counts_all(rig, clock):
    r = rig({"destinations": {DEST: {"unwrap": True, "per_event_results": True}}}, start=True)
    evs = [r.enqueue(dest=DEST, quiet_s=0, max_s=0) for i in range(2)]
    bid = evs[0].batch_id
    stub = r.stub()
    stub.per_event_results = True
    stub.unwrap = True
    stub.script(Outcome.ok("ok"))       # handled=None → all handled
    assert r.d.drain_once() == 1
    assert r.batch(bid)["state"] == "delivered"


def test_per_event_results_filters_unhandled_on_retry(rig, clock):
    r = rig({"destinations": {DEST: {"unwrap": True, "per_event_results": True}}}, start=True)
    evs = [r.enqueue(dest=DEST, quiet_s=0, max_s=0) for i in range(3)]
    bid = evs[0].batch_id
    stub = r.stub()
    stub.per_event_results = True
    stub.unwrap = True
    stub.script(Outcome.ok("ok", handled=[1]), Outcome.ok("ok", handled=[2, 3]))
    assert r.d.drain_once() >= 1
    assert r.batch(bid)["state"] == "delivered"
    calls = stub.calls
    assert len(calls) == 2
    assert [m.get("id") for m in calls[0]["doc"].get("event_meta") or []] == [1, 2, 3]
    assert [m.get("id") for m in calls[1]["doc"].get("event_meta") or []] == [2, 3]


def test_per_event_results_off_is_unaffected(rig, clock):
    r = rig({"destinations": {DEST: {"unwrap": True}}}, start=True)
    evs = [r.enqueue(dest=DEST, quiet_s=0, max_s=0) for i in range(2)]
    bid = evs[0].batch_id
    stub = r.stub()
    stub.script(Outcome.ok("ok"), Outcome.transport("timeout"))
    assert r.d.drain_once() == 1
    assert r.batch(bid)["state"] == "delivered"
    assert r.store.handled_event_ids(bid) == set()
