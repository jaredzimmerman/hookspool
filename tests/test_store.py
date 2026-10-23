"""Contract C1: the durable spool (ARCHITECTURE §3-§5, §7-§9, §17)."""
import json
import multiprocessing
import os
import random
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from conftest import SENTINEL, T0, child_env
from hookspool import config as C
from hookspool import store as store_mod
from hookspool.store import (InvalidEvent, LeaseLost, SchemaError, SpoolError, Store, content_hash, is_ready)

DEST = "hermes:test-route"


def P(**kw):
    return dict(kw, note=SENTINEL)


def cfg(**over):
    return C.merge(C.DEFAULTS, over)


@pytest.fixture
def st(make_store):
    return make_store(cfg())


def rows(path, q, args=()):
    c = sqlite3.connect(path)
    try:
        return c.execute(q, args).fetchall()
    finally:
        c.close()


def enq(st, key="k", payload=None, **kw):
    kw.setdefault("quiet_s", 30)
    kw.setdefault("max_s", 120)
    return st.enqueue(kw.pop("source", "gmail"), key, kw.pop("action", DEST), payload or P(), **kw)


def size_of(payload):
    return len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode())


def consistent(path):
    """Invariants that must hold after any crash."""
    assert rows(path, "PRAGMA integrity_check") == [("ok",)]
    assert rows(path, "PRAGMA foreign_key_check") == []
    for bid, state, cnt, byt, sealed, h, dest in rows(
            path, "SELECT id, state, count, bytes, sealed_at, content_hash, destination FROM batches"):
        n, s = rows(path, "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM events WHERE batch_id=? AND excluded_at"
                          " IS NULL", (bid,))[0]
        assert (cnt, byt) == (n, s), f"batch {bid} counters drifted"
        if state not in ("open", "dropped") and dest != "quarantine":
            assert sealed is not None and h is not None, f"batch {bid} ({state}) not sealed"
    assert rows(path, "SELECT route, key, COUNT(*) FROM batches WHERE state='open' GROUP BY route, key"
                      " HAVING COUNT(*) > 1") == []
    assert rows(path, "SELECT COUNT(*) FROM batches WHERE state='leased' AND (lease_owner IS NULL"
                      " OR lease_until IS NULL)") == [(0,)]


# ---- schema and pragmas -----------------------------------------------------------------------
def test_wal_full_sync_commit_before_return(st):
    r = enq(st)
    c = st._conn()
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c.execute("PRAGMA synchronous").fetchone()[0] == 2           # FULL
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert c.execute("PRAGMA user_version").fetchone()[0] == store_mod.SCHEMA_VERSION
    assert rows(st.path, "SELECT batch_id FROM events") == [(r.batch_id,)]   # another connection sees it
    assert oct(os.stat(st.path).st_mode & 0o777) == "0o600"


def test_schema_check_refuses_missing_older_and_newer(tmp_path):
    p = str(tmp_path / "x" / "spool.db")
    s = Store(p, cfg())
    with pytest.raises(SchemaError, match="migrate"):
        s.check_schema()
    assert not os.path.exists(p)                                          # a probe never creates the file
    os.makedirs(os.path.dirname(p), exist_ok=True)
    sqlite3.connect(p).close()
    with pytest.raises(SchemaError, match="older"):
        s.check_schema()
    assert s.ensure_schema() == [1, 2]
    assert s.ensure_schema() == []                                        # idempotent
    assert s.check_schema() == 2
    assert len(s.instance_id()) == 12
    s._conn().execute("PRAGMA user_version=99")
    with pytest.raises(SchemaError, match="newer"):
        s.check_schema()
    with pytest.raises(SchemaError, match="newer"):
        s.ensure_schema()
    s.close()


def test_migration_files():
    assert [n for n, _ in store_mod.migrations()] == [1, 2] and store_mod.SCHEMA_VERSION == 2
    assert [os.path.basename(p) for _, p in store_mod.migrations()] == ["0001_v2.sql", "0002_unwrap.sql"]


def test_a_schema_1_spool_upgrades_in_place_keeping_its_attempts(tmp_path):
    """0002 adds the unwrap per-event columns to ``attempts``; existing rows keep NULL there."""
    p = str(tmp_path / "old.db")
    c = sqlite3.connect(p)
    for stmt in store_mod._split_sql(open(store_mod.migrations()[0][1], encoding="utf-8").read()):
        c.execute(stmt)
    c.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
    c.execute("PRAGMA user_version=1")
    c.execute("INSERT INTO attempts (batch_id, n, destination, started_at) VALUES (1, 1, 'd', 1.0)")
    c.commit()
    c.close()
    s = Store(p, cfg())
    assert s.ensure_schema() == [2] and s.check_schema() == 2
    assert [(a["batch_id"], a["events_delivered"], a["failed_event_id"]) for a in s.attempts(1)] == [(1, None, None)]


# ---- readiness truth table (§5) -----------------------------------------------------------------
@pytest.mark.parametrize("quiet,mx,first,last,now,ready", [
    (0, 0, 0, 0, 0, True),          # immediate
    (0, 0, 0, 50, 10, True),        # immediate regardless of times
    (30, 120, 0, 0, 29.9, False),   # still in the quiet window
    (30, 120, 0, 0, 30, True),      # quiet window elapsed
    (30, 120, 0, 100, 110, False),  # burst continues, max not reached
    (30, 120, 0, 100, 120, True),   # max wait reached while bursting
    (30, 120, 0, 100, 130, True),   # quiet after last event
    (60, 30, 0, 20, 29, False),     # quiet > max (check warns): max fires first
    (60, 30, 0, 20, 30, True),
    (30, 0, 0, 0, 0, True),         # max 0: ready at once (check warns quiet > max)
    (0, 120, 0, 0, 0, True),        # quiet 0: ready at once
])
def test_readiness_truth_table(quiet, mx, first, last, now, ready):
    b = {"state": "open", "quiet_s": quiet, "max_s": mx, "first_at": T0 + first, "last_at": T0 + last}
    assert is_ready(b, T0 + now) is ready
    for other in ("sealed", "retry", "leased", "dead"):
        assert is_ready(dict(b, state=other), T0 + now) is False


def test_seal_ready_matches_truth_table(make_store, clock):
    st = make_store(cfg())
    cases = [(30, 120, 0), (30, 120, 25), (0, 0, 0), (60, 30, 0)]
    ids = [enq(st, key=f"k{i}", quiet_s=q, max_s=m).batch_id for i, (q, m, _) in enumerate(cases)]
    clock.advance(25)
    enq(st, key="k1", quiet_s=30, max_s=120)                              # k1 bursting
    for t in (0, 29, 30, 54, 55, 120):
        now = T0 + t
        expect = [b for b in ids if st.batch(b)["state"] == "open" and is_ready(st.batch(b), now)]
        assert sorted(st.seal_ready(now)) == sorted(expect)
        for b in expect:
            assert st.batch(b)["state"] == "sealed" and st.batch(b)["sealed_at"] == now
    assert all(st.batch(b)["state"] == "sealed" for b in ids)


# ---- join and seal rules (§5) ---------------------------------------------------------------------
def test_join_single_open_batch_per_group(st, clock):
    a = enq(st, "acct")
    clock.advance(10)
    b = enq(st, "acct")
    assert a.batch_id == b.batch_id and a.event_id != b.event_id
    assert len({enq(st, "acct2").batch_id, enq(st, "acct", route="other-route").batch_id, a.batch_id}) == 3
    bt = st.batch(a.batch_id)
    assert (bt["count"], bt["first_at"], bt["last_at"]) == (2, T0, T0 + 10)
    assert [e["received_at"] for e in st.events(a.batch_id)] == [T0, T0 + 10]


def test_post_join_seal_on_max_events(st):
    ids = [enq(st, max_events=3).batch_id for _ in range(7)]
    assert ids == [ids[0]] * 3 + [ids[3]] * 3 + [ids[6]]
    assert [st.batch(i)["state"] for i in (ids[0], ids[3], ids[6])] == ["sealed", "sealed", "open"]
    assert st.batch(ids[0])["count"] == 3


def test_post_join_seal_on_max_bytes_and_pre_join_seal(st):
    p = P(x="a" * 50)
    s = size_of(p)
    mb = 3 * s + 10
    b1 = enq(st, payload=p, max_bytes=mb).batch_id
    assert enq(st, payload=p, max_bytes=mb).batch_id == b1
    assert enq(st, payload=p, max_bytes=mb).batch_id == b1               # 3s <= mb, still open
    assert st.batch(b1)["state"] == "open"
    big = P(x="a" * 60)                                                    # 3s + size(big) > mb: pre-join seal
    r = enq(st, payload=big, max_bytes=mb)
    assert r.batch_id != b1
    assert st.batch(b1)["state"] == "sealed" and st.batch(b1)["bytes"] == 3 * s <= mb
    # post-join: bytes reaching max_bytes seals in the same transaction
    exact = P(x="y" * 10)
    b2 = enq(st, "k2", payload=exact, max_bytes=size_of(exact)).batch_id
    assert st.batch(b2)["state"] == "sealed"
    # one event larger than max_bytes gets a batch of its own, sealed at once
    huge = enq(st, "k3", payload=P(x="z" * 500), max_bytes=100)
    assert st.batch(huge.batch_id)["state"] == "sealed" and st.batch(huge.batch_id)["count"] == 1


def test_sealed_batch_never_receives_events(st, clock):
    b = enq(st, quiet_s=0, max_s=0).batch_id
    st.seal_ready(clock.t)
    h = st.batch(b)["content_hash"]
    b2 = enq(st, quiet_s=0, max_s=0).batch_id
    assert b2 != b and st.batch(b)["count"] == 1 and st.batch(b)["content_hash"] == h


def test_destination_change_seals_open_batch(st):
    b = enq(st).batch_id
    r = enq(st, action="script:other")
    assert r.batch_id != b and st.batch(b)["state"] == "sealed"
    assert st.batch(r.batch_id)["destination"] == "script:other"


def test_window_copied_into_batch_and_inherited_per_field(make_store, clock):
    c = cfg(sources={"github": {"span": "burst", "max_events": 9}},
            routes=[{"name": "gh-pr", "source": "github", "match": {"class": "pr"}, "destination": DEST,
                     "quiet_s": 60}],
            coalesce={"max_bytes": 5000})
    st = make_store(c)
    b = st.batch(st.enqueue("github", "o/r:pr", DEST, P(), route="gh-pr").batch_id)
    assert (b["quiet_s"], b["max_s"], b["max_events"], b["max_bytes"]) == (60, 600, 9, 5000)
    b = st.batch(st.enqueue("github", "o/r:ci", DEST, P()).batch_id)     # implicit route
    assert (b["quiet_s"], b["max_s"], b["max_events"], b["max_bytes"]) == (120, 600, 9, 5000)
    b = st.batch(st.enqueue("other", "k", DEST, P()).batch_id)
    assert (b["quiet_s"], b["max_s"], b["max_events"], b["max_bytes"]) == (0, 0, 500, 5000)
    # plugin default span is used when the source config names none
    st.span_for = lambda s: "mail" if s == "zoomish" else None
    b = st.batch(st.enqueue("zoomish", "k", DEST, P()).batch_id)
    assert (b["quiet_s"], b["max_s"]) == (30, 120)
    # explicit arguments win; config changes never move an open batch
    b1 = st.enqueue("github", "o/r:x", DEST, P(), quiet_s=5).batch_id
    st.config = cfg(sources={"github": {"quiet_s": 999}})
    assert st.enqueue("github", "o/r:x", DEST, P()).batch_id == b1
    assert st.batch(b1)["quiet_s"] == 5


def test_not_before_only_moves_later(st, clock):
    b = enq(st, not_before=T0 + 100).batch_id
    enq(st, not_before=T0 + 200)
    enq(st, not_before=T0 + 10)
    enq(st)
    assert st.batch(b)["not_before"] == T0 + 200
    st.seal_ready(T0 + 150)
    assert st.batch(b)["state"] == "sealed"
    assert st.candidates(T0 + 199) == [] and [x["id"] for x in st.candidates(T0 + 200)] == [b]


def test_meta_tracks_last_event(st, clock):
    enq(st, source="github")
    clock.advance(5)
    s = st.status()
    assert s["sources"]["github"]["last_event_age_s"] == 5.0 and s["spool"]["last_enqueue_age_s"] == 5.0


# ---- content hash (§3, §6.4) ---------------------------------------------------------------------------
def test_content_hash_stable_across_attempts_and_recomputed(st, clock):
    b = None
    for i in range(4):
        b = enq(st, payload=P(i=i), event_type=["push", "pull"][i % 2], dedup_id=f"d{i}").batch_id
        clock.advance(1)
    st.seal_ready(clock.t + 1000)
    h = st.batch(b)["content_hash"]
    assert h.startswith("sha256:") and len(h) == 71
    d1 = st.document(b, "id-1", 1)
    d2 = st.document(b, "id-2", 2)
    assert d1["content_hash"] == d2["content_hash"] == h == content_hash(d1["events"], d1["event_meta"])
    assert d1["events"] == d2["events"] and d1["delivery_id"] != d2["delivery_id"]
    assert d1["event_types"] == {"pull": 2, "push": 2} and d1["event_type"] == "pull"
    assert d1["summary"] == "4 gmail event(s) for k (pull x2, push x2)"
    # a claim/attempt/retry cycle does not touch it
    st.claim(b, 60, "w1")
    st.mark_retry(b, "w1", "boom", "transport", clock.t, False)
    assert st.batch(b)["content_hash"] == h
    # exclude recomputes; the document omits the event and still matches its hash
    ev = st.events(b)
    assert st.exclude(b, [ev[1]["id"]]) == 1
    h2 = st.batch(b)["content_hash"]
    d3 = st.document(b, "id-3", 3)
    assert h2 != h and h2 == content_hash(d3["events"], d3["event_meta"]) and d3["count"] == 3
    assert ev[1]["id"] not in [m["id"] for m in d3["event_meta"]]
    assert st.batch(b)["count"] == 3
    assert len(st.events(b, include_excluded=True)) == 4                  # still stored


def test_hash_ignores_key_order_and_differs_on_content():
    a = content_hash([{"a": 1, "b": 2}], [{"id": 1}])
    assert a == content_hash([{"b": 2, "a": 1}], [{"id": 1}])
    assert a != content_hash([{"a": 1, "b": 3}], [{"id": 1}])


# ---- claim, candidates, fencing (§6.1, §6.2) -----------------------------------------------------------
def sealed(st, key="k", **kw):
    b = enq(st, key, quiet_s=0, max_s=0, **kw).batch_id
    st.seal_ready(st.now())
    return b


def test_candidates_fifo_head_of_group_only(st, clock):
    a1 = sealed(st, "a")
    clock.advance(1)
    a2 = sealed(st, "a")
    b1 = sealed(st, "b")
    enq(st, "c")                                                          # open head yields nothing
    assert [x["id"] for x in st.candidates()] == [a1, b1]
    assert st.claim(a1, 60, "w")
    assert [x["id"] for x in st.candidates()] == [b1]                     # leased head blocks its group
    st.mark_retry(a1, "w", "x", "transport", clock.t + 30, False)
    assert [x["id"] for x in st.candidates()] == [b1]                     # retry not due blocks
    assert [x["id"] for x in st.candidates(clock.t + 30)] == [a1, b1]
    st.claim(a1, 60, "w2")
    st.mark_dead(a1, "w2", "400", "permanent")
    assert [x["id"] for x in st.candidates(clock.t + 30)] == [a2, b1]     # dead leaves the queue


def test_claim_only_from_sealed_or_retry(st, clock):
    o = enq(st, "open").batch_id
    assert not st.claim(o, 60, "w")
    b = sealed(st)
    assert st.claim(b, 60, "w") and not st.claim(b, 60, "w2")
    bt = st.batch(b)
    assert (bt["state"], bt["lease_owner"], bt["lease_until"]) == ("leased", "w", clock.t + 60)


def test_probe_claim_records_destination(st, clock):
    b = sealed(st)
    assert st.claim(b, 90, "w", probe=True)
    d = st.destination_get(DEST)
    assert d["last_probe_batch_id"] == b and d["probe_until"] == clock.t + 90


def test_attempt_rows_and_delivery_ids(st, clock):
    b = sealed(st)
    st.claim(b, 60, "w")
    a = st.start_attempt(b, "w", per_attempt=False)
    inst = st.instance_id()
    assert a.n == 1 and a.delivery_id == f"hs_{inst}_{b}"
    with pytest.raises(LeaseLost):
        st.start_attempt(b, "other")
    st.mark_retry(b, "w", "refused", "transport", clock.t, False, attempt_id=a.id, status=None, duration_ms=12)
    st.claim(b, 60, "w2")
    a2 = st.start_attempt(b, "w2", per_attempt=True, probe=True)
    assert a2.n == 2 and a2.delivery_id == f"hs_{inst}_{b}_{a2.id}"
    st.mark_delivered(b, "w2", "ok", attempt_id=a2.id, status=200, duration_ms=5)
    at = st.attempts(b)
    assert [(x["outcome"], x["klass"], x["counted"], x["probe"]) for x in at] == \
        [("retry", "transport", 0, 0), ("ok", None, 1, 1)]
    assert st.batch(b)["attempts"] == 1                                   # transport retry was uncounted


def test_counted_and_uncounted_marks(st, clock):
    b = sealed(st)
    for i, (klass, counted) in enumerate([("transport", False), ("throttle", False), ("batch", True),
                                           ("config", False), ("batch", True)]):
        st.claim(b, 60, f"w{i}")
        st.mark_retry(b, f"w{i}", "e", klass, clock.t, counted)
    bt = st.batch(b)
    assert (bt["state"], bt["attempts"], bt["last_class"]) == ("retry", 2, "batch")
    with pytest.raises(ValueError):
        st.claim(b, 60, "x") and st.mark_retry(b, "x", "e", "permanent", clock.t, True)


def test_fenced_mark_discards_lost_lease(st, clock):
    b = sealed(st)
    assert st.claim(b, 60, "w1")
    a = st.start_attempt(b, "w1")
    # the lease expires and nobody in this process holds it: recovered as an uncounted retry
    assert st.recover_leases(clock.t + 61, live_batch_ids=[]) == [b]
    bt = st.batch(b)
    assert (bt["state"], bt["attempts"], bt["last_error"]) == ("retry", 0, "lease expired")
    assert st.attempts(b)[0]["outcome"] == "retry"
    st.claim(b, 60, "w2", now=clock.t + 61)
    a2 = st.start_attempt(b, "w2")
    # the first worker reports late: nothing applied, its emits are not enqueued
    with pytest.raises(LeaseLost):
        st.mark_delivered(b, "w1", "ok", emits=[{"source": "s", "key": "k", "action": DEST, "payload": {}}],
                          attempt_id=a.id)
    with pytest.raises(LeaseLost):
        st.mark_retry(b, "w1", "x", "batch", clock.t, True)
    with pytest.raises(LeaseLost):
        st.mark_dead(b, "w1", "x", "permanent")
    assert st.batch(b)["state"] == "leased" and st.batch(b)["lease_owner"] == "w2"
    assert rows(st.path, "SELECT COUNT(*) FROM events WHERE source='s'") == [(0,)]
    st.mark_delivered(b, "w2", "ok", attempt_id=a2.id)
    at = {x["id"]: x["outcome"] for x in st.attempts(b)}
    assert at == {a.id: "retry", a2.id: "ok"}                            # finished once, by recovery


def test_lost_lease_attempt_finished_discarded(st, clock):
    b = sealed(st)
    st.claim(b, 60, "w1")
    a = st.start_attempt(b, "w1")
    st._write(lambda c: c.execute("UPDATE batches SET lease_owner='thief' WHERE id=?", (b,)))
    with pytest.raises(LeaseLost):
        st.mark_retry(b, "w1", "x", "transport", clock.t, False, attempt_id=a.id)
    x = st.attempts(b)[0]
    assert (x["outcome"], x["counted"]) == ("discarded", 0)


def test_recover_leases_never_touches_live_workers(st, clock):
    b = sealed(st)
    st.claim(b, 60, "w1")
    assert st.recover_leases(clock.t + 61, live_batch_ids=[b]) == []
    assert st.recover_leases(clock.t + 30) == []                          # not expired
    assert st.recover_leases(clock.t, all_leases=True) == [b]            # startup: every lease is dead


def test_mark_delivered_rejects_bad_emits_before_writing(st, clock):
    b = sealed(st)
    st.claim(b, 60, "w")
    with pytest.raises(InvalidEvent):
        st.mark_delivered(b, "w", "ok", emits=[{"source": "s", "key": "k", "payload": {}, "bogus": 1}])
    with pytest.raises(InvalidEvent):
        st.mark_delivered(b, "w", "ok", emits=[{"source": "s", "key": "k", "payload": {}}])   # no destination
    assert st.batch(b)["state"] == "leased"
    out = st.mark_delivered(b, "w", "ok", emits=[
        {"source": "gmail", "key": "acct", "action": "hermes:mail", "payload": P(a=1), "dedup_id": "m1"},
        {"source": "gmail", "key": "acct", "action": "hermes:mail", "payload": P(a=1), "dedup_id": "m1"}])
    assert [o.duplicate for o in out] == [False, True]
    assert st.batch(b)["state"] == "delivered"


def test_emit_route_resolves_destination(make_store):
    st = make_store(cfg(sources={"gmail": {"action": "script:gmail_delta"}},
                        routes=[{"name": "gmail-mail", "source": "gmail", "destination": "hermes:mail"}]))
    b = sealed(st)
    st.claim(b, 60, "w")
    out = st.mark_delivered(b, "w", "ok", emits=[{"source": "gmail", "key": "a", "route": "gmail-mail",
                                                  "payload": {}},
                                                 {"source": "gmail", "key": "a", "payload": {}}])
    assert [st.batch(o.batch_id)["destination"] for o in out] == ["hermes:mail", "script:gmail_delta"]


def test_redue_config_retries_and_retry_now(st, clock):
    b = sealed(st, "a")
    c = sealed(st, "b")
    st.claim(b, 60, "w")
    st.mark_retry(b, "w", "401", "config", clock.t + 300, False)
    st.claim(c, 60, "w")
    st.mark_retry(c, "w", "refused", "transport", clock.t + 300, False)
    assert st.redue_config_retries(clock.t) == 1
    assert st.batch(b)["next_attempt_at"] == clock.t and st.batch(c)["next_attempt_at"] == clock.t + 300
    assert st.retry_now(c) and st.batch(c)["next_attempt_at"] == clock.t
    assert not st.retry_now(c + 100)


def test_mark_dead_unleased_for_poison(st):
    b = sealed(st)
    assert st.mark_dead_unleased(b, "poison: probe B succeeded", "poison")
    assert st.batch(b)["last_class"] == "poison" and not st.mark_dead_unleased(b, "x")


# ---- dedup (§7) -----------------------------------------------------------------------------------
def test_dedup_hit_is_noop(st, clock):
    r1 = enq(st, payload=P(n=1), dedup_id="d-1")
    clock.advance(5)
    r2 = enq(st, payload=P(n=2), dedup_id="d-1")
    assert (r2.event_id, r2.batch_id, r2.duplicate, r2.replayed) == (r1.event_id, r1.batch_id, True, False)
    assert [e["payload"]["n"] for e in st.events(r1.batch_id)] == [1]
    assert st.batch(r1.batch_id)["last_at"] == T0                       # window not extended
    assert rows(st.path, "SELECT dup_count FROM dedup") == [(1,)]
    assert not enq(st, source="plaid", dedup_id="d-1").duplicate       # per source


def test_dedup_survives_delivery_and_window_expiry(make_store, clock):
    st = make_store(cfg(sources={"plaid": {"dedup_window_s": 600}}))
    r = enq(st, source="plaid", dedup_id="h1", quiet_s=0, max_s=0)
    st.seal_ready()
    st.claim(r.batch_id, 60, "w")
    st.mark_delivered(r.batch_id, "w", "ok")
    assert enq(st, source="plaid", dedup_id="h1").duplicate             # delivered: still a duplicate
    st.prune(clock.t + 599)
    assert enq(st, source="plaid", dedup_id="h1").duplicate
    st.prune(clock.t + 601)                                               # window over: id reopens
    clock.advance(601)
    r2 = enq(st, source="plaid", dedup_id="h1")
    assert not r2.duplicate and r2.event_id != r.event_id


def test_dedup_hit_on_dead_batch_replays_it(st, clock):
    r = enq(st, dedup_id="g1", quiet_s=0, max_s=0)
    st.seal_ready()
    st.claim(r.batch_id, 60, "w")
    st.mark_dead(r.batch_id, "w", "422", "permanent", counted=True)
    st.issue_open(f"delivery:{DEST}:permanent", "delivery", destination=DEST)
    clock.advance(10)
    r2 = enq(st, dedup_id="g1")
    assert (r2.event_id, r2.batch_id, r2.replayed, r2.duplicate) == (r.event_id, r.batch_id, True, False)
    b = st.batch(r.batch_id)
    assert (b["state"], b["attempts"], b["next_attempt_at"], b["not_before"]) == ("retry", 0, clock.t, clock.t)
    assert st.issue_get(f"delivery:{DEST}:permanent")["state"] == "resolved"
    assert b["count"] == 1                                                # content stays frozen


def test_dedup_hit_on_dropped_batch_enqueues_anew(st, clock):
    r = enq(st, dedup_id="t1")
    assert st.drop(r.batch_id, reason="test")
    clock.advance(3)
    r2 = enq(st, dedup_id="t1")
    assert not r2.duplicate and not r2.replayed and r2.batch_id != r.batch_id
    assert rows(st.path, "SELECT event_id, seen_at FROM dedup") == [(r2.event_id, clock.t)]
    assert enq(st, dedup_id="t1").duplicate


# ---- quarantine (§9) ---------------------------------------------------------------------------------
def test_quarantine_and_reclassify(st, clock):
    raw = json.dumps({"note": SENTINEL, "k": "acct"}).encode()
    hdrs = {"X-Signature": "secret-token-XYZ", "Content-Type": "application/json"}
    eid, bid = st.quarantine("gmail", raw, hdrs, "plugin error: KeyError", auth_headers=("x-signature",))
    b = st.batch(bid)
    assert (b["state"], b["destination"], b["key"], b["route"], b["last_class"]) == \
        ("dead", "quarantine", "quarantine", "gmail", "quarantine")
    assert b["last_error"] == "plugin error: KeyError"
    ev = rows(st.path, "SELECT payload, raw, headers FROM events WHERE id=?", (eid,))[0]
    assert ev[0] is None and ev[1] == raw and json.loads(ev[2]) == {"Content-Type": "application/json"}
    with open(st.path, "rb") as f:
        assert b"secret-token-XYZ" not in f.read()
    st._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    with open(st.path, "rb") as f:
        assert b"secret-token-XYZ" not in f.read()
    iss = st.issue_get("config:plugin:gmail")
    assert iss["state"] == "open" and iss["sample_batch_id"] == bid
    assert st.status()["batches"]["quarantined"] == 1 and st.status()["batches"]["dead"] == 0
    assert st.replay_dead() == []                                         # bulk replay never re-sends these
    with pytest.raises(ValueError, match="reclassify"):
        st.replay(bid)

    def broken(src, body, headers):
        raise KeyError("still broken")
    with pytest.raises(KeyError):
        st.replay(bid, reclassify=broken)
    assert st.batch(bid)["state"] == "dead"

    def fixed(src, body, headers):
        d = json.loads(body)
        assert headers == {"Content-Type": "application/json"}
        return {"key": d["k"], "action": DEST, "payload": d, "event_type": "push", "dedup_id": "q1"}
    assert st.replay(bid, reclassify=fixed)
    b = st.batch(bid)
    assert (b["state"], b["result"]) == ("delivered", "reclassified")
    new = rows(st.path, "SELECT batch_id, raw FROM events WHERE dedup_id='q1'")
    assert len(new) == 1 and new[0][1] == raw and st.batch(new[0][0])["destination"] == DEST
    assert st.issue_get("config:plugin:gmail")["state"] == "resolved"


def test_reclassify_can_ignore_and_drop_resolves(st):
    _, b1 = st.quarantine("zoom", b"{}", {}, "plugin error: ValueError")
    _, b2 = st.quarantine("zoom", b"{}", {}, "plugin error: ValueError")
    assert st.reclassify_quarantine(b1, lambda s, r, h: None) == []
    assert st.issue_get("config:plugin:zoom")["state"] == "open"         # b2 still quarantined
    assert st.drop(b2)
    assert st.issue_get("config:plugin:zoom")["state"] == "resolved"


# ---- operator actions ---------------------------------------------------------------------------------
def test_replay_dead_resets_and_bulk(st, clock):
    bs = []
    for k in ("a", "b", "c"):
        b = sealed(st, k)
        st.claim(b, 60, "w")
        st.mark_dead(b, "w", "boom", "batch", counted=True)
        bs.append(b)
    assert st.replay(bs[0]) and not st.replay(bs[0])
    assert st.replay_dead(source="nope") == []
    assert sorted(st.replay_dead(destination=DEST)) == bs[1:]
    assert all(st.batch(b)["state"] == "retry" and st.batch(b)["attempts"] == 0 for b in bs)


def test_split_variants(st, clock):
    b = None
    for i in range(6):
        b = enq(st, payload=P(i=i), max_events=10).batch_id
        clock.advance(1)
    st.seal_ready(clock.t + 500)
    assert st.batch(b)["state"] == "sealed"
    with pytest.raises(ValueError):
        st.split(b)
    with pytest.raises(ValueError):
        st.split(b, max_events=6)                                         # nothing to split
    parts = st.split(b, max_events=4, now=clock.t)
    assert len(parts) == 2
    orig = st.batch(b)
    assert (orig["state"], orig["result"], orig["count"]) == ("dropped", f"split into {parts[0]},{parts[1]}", 0)
    p1, p2 = st.batch(parts[0]), st.batch(parts[1])
    assert (p1["count"], p2["count"], p1["state"], p1["not_before"]) == (4, 2, "sealed", clock.t)
    assert p1["first_at"] == T0 and p1["last_at"] == T0 + 3 and p2["first_at"] == T0 + 4
    for p in parts:
        d = st.document(p, "x", 1)
        assert st.batch(p)["content_hash"] == content_hash(d["events"], d["event_meta"])
    assert [x["id"] for x in st.candidates(clock.t)] == [parts[0]]       # FIFO kept: first part first
    ids = [e["id"] for e in st.events(parts[0])]
    assert st.split(parts[0], at=ids[2])
    sz = st.batch(parts[1])["bytes"]
    with pytest.raises(ValueError):
        st.split(parts[1], at=ids[0])
    q = st.split(parts[1], max_bytes=sz // 2 + 1)
    assert len(q) == 2
    consistent(st.path)


def test_split_dead_batch_and_refusals(st):
    b = sealed(st)
    enq(st, quiet_s=0, max_s=0)
    st.claim(b, 60, "w")
    with pytest.raises(ValueError):
        st.split(b, max_events=1)                                         # leased
    st.mark_dead(b, "w", "413", "permanent")
    with pytest.raises(ValueError):
        st.split(b, max_events=1)                                         # one event: nothing to split
    _, qb = st.quarantine("s", b"x", {}, "e")
    with pytest.raises(ValueError):
        st.split(qb, max_events=1)


def test_exclude_rules(st):
    b = enq(st).batch_id
    e1 = enq(st).event_id
    other = enq(st, "other").event_id
    with pytest.raises(ValueError):
        st.exclude(b, [other])
    assert st.exclude(b, [e1]) == 1 and st.exclude(b, [e1]) == 0
    assert st.batch(b)["count"] == 1 and st.batch(b)["content_hash"] is None   # open: no hash yet
    st.seal_ready(T0 + 1000)
    d = st.document(b, "x", 1)
    assert d["count"] == 1 and st.batch(b)["content_hash"] == content_hash(d["events"], d["event_meta"])
    consistent(st.path)


def test_drop_rules(st):
    b = sealed(st)
    st.claim(b, 60, "w")
    assert not st.drop(b)                                                 # leased
    st.mark_delivered(b, "w", "ok")
    assert not st.drop(b)                                                 # delivered
    o = enq(st, "x").batch_id
    assert st.drop(o, reason="operator") and st.batch(o)["result"] == "dropped: operator"
    assert enq(st, "x").batch_id != o


# ---- destinations, issues, alerts ---------------------------------------------------------------------------
def test_destination_row(st):
    assert st.destination_get("hermes:x")["state"] == "closed"
    d = st.destination_set("hermes:x", state="paused", paused_by="op", paused_at=T0)
    assert d["state"] == "paused" and d["paused_by"] == "op"
    with pytest.raises(ValueError):
        st.destination_set("hermes:x", inflight=3)
    with pytest.raises(ValueError):
        st.destination_set("hermes:x", state="dead")
    assert [x["name"] for x in st.destination_list()] == ["hermes:x"]


def test_issue_lifecycle(st, clock):
    i, new = st.issue_open("delivery:hermes:x:unreachable", "delivery", destination="hermes:x", error="refused")
    assert new and st.issues_to_alert()[0]["id"] == i
    st.issue_mark_alerted(i)
    assert st.issue_open("delivery:hermes:x:unreachable", "delivery") == (i, False)
    assert st.issues_to_alert() == [] and st.issue_get(i)["count"] == 2
    assert st.issue_ack(i) and st.issue_get(i)["state"] == "acked"
    assert st.issue_resolve("delivery:hermes:x:unreachable")
    assert st.issue_open("delivery:hermes:x:unreachable", "delivery") == (i, True)   # reopen alerts again
    assert st.issue_get(i)["alerted_at"] is None
    assert st.issue_ignore(i)
    assert st.issue_open("delivery:hermes:x:unreachable", "delivery") == (i, False)
    assert st.issue_get(i)["state"] == "ignored" and st.issues_to_alert() == []
    with pytest.raises(sqlite3.IntegrityError):
        st._conn().execute("INSERT INTO issues (fingerprint, kind, first_at, last_at) VALUES ('x', 'bogus', 0, 0)")


def test_alert_gate(st, clock):
    assert st.alert_gate("delivery", 1800) == (True, 0)
    assert st.alert_gate("delivery", 1800) == (False, 1)
    assert st.alert_gate("delivery", 1800) == (False, 2)
    clock.advance(1800)
    assert st.alert_gate("delivery", 1800) == (True, 2)


# ---- retention (§8) ------------------------------------------------------------------------------------------
def test_retention_bounds(make_store, clock):
    st = make_store(cfg(sources={"plaid": {"dedup_window_s": 600}}))
    delivered_old = sealed(st, "a", dedup_id="g-old")
    st.claim(delivered_old, 60, "w")
    a_old = st.start_attempt(delivered_old, "w")
    st.mark_delivered(delivered_old, "w", "ok", attempt_id=a_old.id)
    dead_old = sealed(st, "b")
    st.claim(dead_old, 60, "w")
    st.mark_dead(dead_old, "w", "400", "permanent")
    dropped_old = enq(st, "c").batch_id
    st.drop(dropped_old)
    enq(st, "p", source="plaid", dedup_id="h")
    st.issue_open("x:old", "spool")
    st.issue_resolve("x:old")
    st.issue_open("x:open", "spool")
    _, qb = st.quarantine("s", b"x", {}, "e")

    clock.advance(6 * 86400)                                              # only the dedup windows have passed
    delivered_new = sealed(st, "d")
    st.claim(delivered_new, 60, "w")
    st.mark_delivered(delivered_new, "w", "ok")
    out = st.prune()
    assert out["dedup"] == 2 and out["delivered"] == 0                   # plaid 600 s, gmail 24 h
    clock.advance(1.01 * 86400)                                           # 7 days + after the first batches
    out = st.prune()
    assert (out["delivered"], out["dropped"]) == (1, 1)
    assert st.batch(delivered_old) is None and st.batch(dropped_old) is None
    assert st.batch(delivered_new) is not None
    assert st.batch(dead_old)["state"] == "dead" and st.batch(qb)["state"] == "dead"   # never pruned
    assert rows(st.path, "SELECT COUNT(*) FROM events WHERE batch_id=?", (delivered_old,)) == [(0,)]
    assert rows(st.path, "SELECT COUNT(*) FROM attempts WHERE id=?", (a_old.id,)) == [(1,)]  # 30 d
    clock.advance(24 * 86400)
    out = st.prune()
    assert out["attempts"] == 1
    clock.advance(70 * 86400)
    out = st.prune()
    assert out["issues"] == 1
    assert st.issue_get("x:old") is None and st.issue_get("x:open") is not None
    assert st.batch(dead_old) is not None
    consistent(st.path)


def test_prune_in_small_transactions(make_store, clock):
    st = make_store(cfg(), durable=False)
    seen = []
    old = store_mod._test_hook
    store_mod._test_hook = lambda p: seen.append(p) if p == "prune:mid" else None
    try:
        def fn(c):
            for i in range(2500):
                c.execute("INSERT INTO batches (route, source, destination, key, state, first_at, last_at, quiet_s,"
                          " max_s, max_events, max_bytes, count, bytes, delivered_at, sealed_at, content_hash)"
                          " VALUES ('r', 's', 'd', ?, 'delivered', 0, 0, 0, 0, 1, 1, 1, 1, 0, 0, 'h')", (f"k{i}",))
                c.execute("INSERT INTO events (source, route, batch_id, received_at, payload, size)"
                          " VALUES ('s', 'r', last_insert_rowid(), 0, '{}', 1)")
        st._write(fn)
        out = st.prune(clock.t, chunk=1000)
    finally:
        store_mod._test_hook = old
    assert out["delivered"] == 2500 and out["events"] == 2500 and len(seen) == 3


# ---- status and export --------------------------------------------------------------------------------
def test_status_shape(make_store, clock):
    st = make_store(cfg(sources={"gcal": {"expect_every_s": 100}}))
    enq(st, "a")
    b = sealed(st, "b")
    st.claim(b, 60, "w")
    st.mark_dead(b, "w", "x", "permanent")
    st.destination_set("hermes:test-route", state="open", opened_at=T0, cooldown_s=30)
    st.meta_set({"dispatcher_heartbeat_at": clock.t - 5, "dispatcher_pid": 42})
    clock.advance(10)
    s = st.status()
    assert s["batches"]["open"] == 1 and s["batches"]["dead"] == 1
    assert s["oldest_pending_age_s"] == 10.0 and s["schema"] == store_mod.SCHEMA_VERSION
    assert s["dispatcher"] == {"alive": True, "heartbeat_age_s": 15.0, "pid": 42}
    assert s["destinations"][DEST]["state"] == "open" and s["destinations"][DEST]["cooldown_s"] == 30
    assert s["sources"]["gcal"] == {"last_event_age_s": None, "expected_every_s": 100, "silent": True}
    assert "1 dead batch(es)" in s["degraded"] and "source gcal silent" in s["degraded"]
    assert any("breaker open" in d for d in s["degraded"])
    assert s["spool_bytes"] > 0 and s["disk_free_bytes"] > 0
    json.dumps(s)


def test_export_roundtrip(st):
    r = enq(st, raw=b"\x00raw\xff", headers={"X-GitHub-Event": "push"})
    st.quarantine("s", b"q", {"A": "b"}, "e")
    out = list(st.export())
    assert len(out) == 2
    ev = out[0]["events"][0]
    import base64
    assert base64.b64decode(ev["raw"]) == b"\x00raw\xff" and ev["headers"] == {"X-GitHub-Event": "push"}
    assert ev["payload"]["note"] == SENTINEL and out[0]["id"] == r.batch_id
    assert [b["id"] for b in st.export(state="dead")] == [out[1]["id"]]


def test_store_raw_false_drops_raw(make_store):
    st = make_store(cfg(spool={"store_raw": False}, sources={"keep": {"store_raw": True}}))
    a = st.enqueue("gmail", "k", DEST, {}, raw=b"x")
    b = st.enqueue("keep", "k", DEST, {}, raw=b"x")
    assert rows(st.path, "SELECT id, raw FROM events ORDER BY id") == [(a.event_id, None), (b.event_id, b"x")]


# ---- errors ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kw", [
    dict(payload=["not", "a", "dict"]), dict(payload={"x": object()}), dict(payload={"x": float("nan")}),
    dict(action="quarantine"), dict(action="has space"), dict(action=""), dict(key=""), dict(key="k" * 513),
    dict(quiet_s="5"), dict(max_events=0), dict(max_bytes=1.5), dict(event_type=5), dict(route=""),
    dict(headers={"a": 1}), dict(dedup_id={"a": 1}), dict(not_before=True),
])
def test_bad_input_raises_invalid_event(st, kw):
    args = dict(source="gmail", key="k", action=DEST, payload={"a": 1})
    args.update(kw)
    with pytest.raises(InvalidEvent):
        st.enqueue(args.pop("source"), args.pop("key"), args.pop("action"), args.pop("payload"), **args)
    assert rows(st.path, "SELECT COUNT(*) FROM batches") == [(0,)]
    assert not issubclass(InvalidEvent, SpoolError)                      # the intake quarantines, never 503s


def test_unwritable_spool_raises_spoolerror(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    s = Store(str(blocker / "spool.db"), cfg())                          # parent is a file
    with pytest.raises(SpoolError):
        s.enqueue("gmail", "k", DEST, {"a": 1})


def test_busy_spool_raises_spoolerror(make_store):
    st = make_store(cfg())
    enq(st)
    st.close()
    ro = Store(st.path, cfg(), busy_timeout_s=0.2)
    ro._conn()
    hold = sqlite3.connect(st.path, timeout=0.1, isolation_level=None)
    hold.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(SpoolError):
            enq(ro)
    finally:
        hold.execute("ROLLBACK")
        hold.close()
        ro.close()


def test_lock_helper(tmp_path):
    p = str(tmp_path / "dispatch.lock")
    f = store_mod.acquire_lock(p)
    code = "import sys; from hookspool.store import acquire_lock, LockHeld\ntry:\n acquire_lock(sys.argv[1])\n" \
           "except LockHeld:\n sys.exit(3)\n"
    r = subprocess.run([sys.executable, "-c", code, p], env=child_env(), timeout=60)
    assert r.returncode == 3
    f.close()
    assert subprocess.run([sys.executable, "-c", code, p], env=child_env(), timeout=60).returncode == 0


# ---- concurrency: 4 writer processes -------------------------------------------------------------------
def test_four_writer_processes(make_store):
    import _workers
    st = make_store(cfg())
    db = st.path
    st.close()
    n, me, mb = 1000, 37, 4000
    ctx = multiprocessing.get_context("spawn")
    with ctx.Manager() as m:
        go = m.Event()
        with ctx.Pool(4) as pool:
            res = [pool.apply_async(_workers.enqueue_many, (db, w, n, me, mb, go)) for w in range(4)]
            time.sleep(0.5)
            go.set()
            assert [r.get(timeout=300) for r in res] == [n] * 4
    got = [r[0] for r in rows(db, "SELECT dedup_id FROM events")]
    assert sorted(got) == sorted(f"w{w}-{i}" for w in range(4) for i in range(n))
    consistent(db)
    for cnt, byt in rows(db, "SELECT count, bytes FROM batches"):
        assert cnt <= me and (byt <= mb or cnt == 1)
    # per-key order: event ids increase with each worker's own sequence
    for w in range(4):
        seq = [json.loads(p)["i"] for (p,) in rows(db, "SELECT payload FROM events WHERE dedup_id LIKE ?"
                                                       " ORDER BY id", (f"w{w}-%",))]
        assert seq == sorted(seq)


# ---- crash safety: kill -9 at every hook point ------------------------------------------------------------
KILL_CHILD = r"""
import json, os, signal, sys
from hookspool import store, config
db, scenario, point, kill_on = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
T = [1800000000.0]
st = store.Store(db, config.DEFAULTS, now=lambda: T[0])
seen = {"n": 0}
def hook(p):
    if p == point:
        seen["n"] += 1
        if seen["n"] == kill_on:
            os.kill(os.getpid(), signal.SIGKILL)
def say(*a):
    print(*a, flush=True)
D = "hermes:r"
def enq(key, i, **kw):
    kw.setdefault("quiet_s", 30); kw.setdefault("max_s", 120)
    r = st.enqueue("gmail", key, D, {"i": i, "pad": "x" * 40}, dedup_id="e%d" % i, **kw)
    say("enq", i, r.batch_id)
if scenario == "enqueue":
    store._test_hook = hook
    enq("a", 1); enq("a", 2); enq("b", 3, max_events=2); enq("b", 4, max_events=2)
    enq("c", 5, max_bytes=60); enq("c", 6, max_bytes=60); enq("a", 7)
elif scenario == "seal":
    for i, k in enumerate("abc"):
        enq(k, i)
    store._test_hook = hook
    say("sealed", *st.seal_ready(T[0] + 500))
elif scenario == "claim":
    enq("a", 1, quiet_s=0, max_s=0); st.seal_ready()
    store._test_hook = hook
    say("claimed", st.claim(1, 60, "w1"))
elif scenario == "deliver":
    enq("a", 1, quiet_s=0, max_s=0); st.seal_ready(); st.claim(1, 60, "w1")
    a = st.start_attempt(1, "w1")
    store._test_hook = hook
    st.mark_delivered(1, "w1", "ok", emits=[{"source": "gmail", "key": "z", "action": "hermes:z",
                                              "payload": {"emit": 1}, "dedup_id": "emit-1"}], attempt_id=a.id)
    say("delivered")
elif scenario == "prune":
    for i in range(3):
        enq("k%d" % i, i, quiet_s=0, max_s=0)
    st.seal_ready()
    for b in (1, 2, 3):
        st.claim(b, 60, "w"); st.mark_delivered(b, "w", "ok")
    T[0] += 30 * 86400
    store._test_hook = hook
    say("pruned", json.dumps(st.prune(chunk=1)))
"""


def kill_child(db, scenario, point, kill_on):
    p = subprocess.run([sys.executable, "-c", KILL_CHILD, db, scenario, point, str(kill_on)],
                       env=child_env(), capture_output=True, text=True, timeout=60)
    assert p.returncode == -signal.SIGKILL, (p.returncode, p.stdout, p.stderr)
    return [line.split() for line in p.stdout.splitlines()]


@pytest.fixture
def fresh_db(make_store):
    st = make_store(C.DEFAULTS)
    st.close()
    return st.path


@pytest.mark.parametrize("point,kill_on,acked,sealed_after", [
    ("enqueue:after_batch", 3, 2, 0),      # 3rd enqueue opens batch b: batch row inserted, event not
    ("enqueue:after_event", 2, 1, 0),      # 2nd enqueue joins a: event written, not committed
    ("enqueue:after_seal", 1, 3, 0),       # 4th enqueue post-join seals b (max_events 2)
    ("enqueue:after_seal", 2, 5, 1),       # 6th enqueue pre-join seals c (max_bytes); b stays sealed
])
def test_kill9_inside_enqueue(fresh_db, point, kill_on, acked, sealed_after):
    out = kill_child(fresh_db, "enqueue", point, kill_on)
    assert len(out) == acked
    consistent(fresh_db)
    stored = sorted(int(r[0][1:]) for r in rows(fresh_db, "SELECT dedup_id FROM events"))
    assert stored == [int(o[1]) for o in out]                             # every acked event, nothing else
    assert rows(fresh_db, "SELECT COUNT(*) FROM dedup") == [(acked,)]
    assert rows(fresh_db, "SELECT COUNT(*) FROM batches WHERE state='sealed'") == [(sealed_after,)]
    st = Store(fresh_db, C.DEFAULTS)                                      # the spool keeps working afterwards
    st.enqueue("gmail", "a", "hermes:r", {"after": 1})
    consistent(fresh_db)
    st.close()


def test_kill9_mid_seal_ready(fresh_db):
    out = kill_child(fresh_db, "seal", "seal:mid", 1)
    assert len(out) == 3
    consistent(fresh_db)
    assert rows(fresh_db, "SELECT state, COUNT(*) FROM batches GROUP BY state") == [("open", 3)]
    st = Store(fresh_db, C.DEFAULTS)
    assert len(st.seal_ready(T0 + 500)) == 3
    consistent(fresh_db)
    st.close()


def test_kill9_mid_claim(fresh_db):
    kill_child(fresh_db, "claim", "claim:after_update", 1)
    consistent(fresh_db)
    assert rows(fresh_db, "SELECT state, lease_owner FROM batches") == [("sealed", None)]


def test_kill9_between_emits_and_mark(fresh_db):
    kill_child(fresh_db, "deliver", "deliver:after_emits", 1)
    consistent(fresh_db)
    assert rows(fresh_db, "SELECT state, lease_owner FROM batches WHERE id=1") == [("leased", "w1")]
    assert rows(fresh_db, "SELECT COUNT(*) FROM events WHERE dedup_id='emit-1'") == [(0,)]
    assert rows(fresh_db, "SELECT outcome FROM attempts") == [(None,)]
    st = Store(fresh_db, C.DEFAULTS, now=lambda: T0 + 1)
    assert st.recover_leases(all_leases=True) == [1]                      # dispatcher restart
    assert st.claim(1, 60, "w2")
    a = st.start_attempt(1, "w2")
    st.mark_delivered(1, "w2", "ok", emits=[{"source": "gmail", "key": "z", "action": "hermes:z",
                                              "payload": {"emit": 1}, "dedup_id": "emit-1"}], attempt_id=a.id)
    assert rows(fresh_db, "SELECT COUNT(*) FROM events WHERE dedup_id='emit-1'") == [(1,)]   # exactly once
    assert rows(fresh_db, "SELECT outcome FROM attempts ORDER BY id") == [("retry",), ("ok",)]
    consistent(fresh_db)
    st.close()


def test_kill9_mid_prune(fresh_db):
    out = kill_child(fresh_db, "prune", "prune:mid", 2)                   # second 1-row chunk
    assert out[-1][0] == "enq"                                            # prune never returned
    consistent(fresh_db)
    # first chunk committed (batch 1 and its event gone), second rolled back whole
    assert rows(fresh_db, "SELECT id FROM batches ORDER BY id") == [(2,), (3,)]
    assert rows(fresh_db, "SELECT batch_id FROM events ORDER BY id") == [(2,), (3,)]


LOOP_CHILD = r"""
import sys
from hookspool.store import Store
from hookspool.config import DEFAULTS
st = Store(sys.argv[1], DEFAULTS)
i = 0
while True:
    st.enqueue("gmail", "k%d" % (i % 3), "hermes:r", {"i": i, "pad": "x" * 2000}, dedup_id="e%d" % i,
               max_events=7)
    print(i, flush=True)
    i += 1
"""


def test_kill9_at_random_point_committed_events_survive(fresh_db):
    p = subprocess.Popen([sys.executable, "-c", LOOP_CHILD, fresh_db], env=child_env(),
                         stdout=subprocess.PIPE, text=True)
    acked = []
    for line in p.stdout:
        acked.append(int(line))
        if len(acked) >= 150:
            break
    p.send_signal(signal.SIGKILL)
    p.wait(30)
    consistent(fresh_db)
    stored = {r[0] for r in rows(fresh_db, "SELECT dedup_id FROM events")}
    assert {f"e{i}" for i in acked} <= stored


# ---- model test (§18) ----------------------------------------------------------------------------------
class Model:
    """A Python model of the batch state machine, join/seal rules and dedup."""

    def __init__(self):
        self.b = {}
        self.ev = {}
        self.dedup = {}
        self.nb = self.ne = 0

    def _seal(self, bid):
        self.b[bid]["state"] = "sealed"

    def enqueue(self, now, route, key, dest, size, dd, w):
        if dd is not None and dd in self.dedup:
            eid = self.dedup[dd]
            bid = self.ev[eid]
            st = self.b[bid]["state"]
            if st == "dead":
                self.b[bid].update(state="retry", next=now, nbf=now, attempts=0)
                return eid, bid, False, True
            if st != "dropped":
                return eid, bid, True, False
        o = [i for i, x in self.b.items() if x["state"] == "open" and (x["route"], x["key"]) == (route, key)]
        assert len(o) <= 1
        bid = o[0] if o else None
        if bid is not None:
            x = self.b[bid]
            if x["dest"] != dest or (x["count"] >= 1 and x["bytes"] + size > x["mb"]):
                self._seal(bid)
                bid = None
        if bid is None:
            self.nb += 1
            bid = self.nb
            self.b[bid] = dict(state="open", route=route, key=key, dest=dest, first=now, last=now, q=w[0], m=w[1],
                               me=w[2], mb=w[3], nbf=None, count=0, bytes=0, next=None, attempts=0, owner=None)
        x = self.b[bid]
        x["last"] = max(x["last"], now)
        x["count"] += 1
        x["bytes"] += size
        self.ne += 1
        self.ev[self.ne] = bid
        if dd is not None:
            self.dedup[dd] = self.ne
        if x["count"] >= x["me"] or x["bytes"] >= x["mb"]:
            self._seal(bid)
        return self.ne, bid, False, False

    def seal_ready(self, now):
        out = []
        for i, x in sorted(self.b.items()):
            if x["state"] == "open" and is_ready({"state": "open", "quiet_s": x["q"], "max_s": x["m"],
                                                  "first_at": x["first"], "last_at": x["last"]}, now):
                self._seal(i)
                out.append(i)
        return out

    def candidates(self, now):
        groups = {}
        for i, x in self.b.items():
            if x["state"] in ("open", "sealed", "retry", "leased"):
                g = (x["route"], x["key"])
                if g not in groups or (x["first"], i) < (self.b[groups[g]]["first"], groups[g]):
                    groups[g] = i
        out = []
        for i in groups.values():
            x = self.b[i]
            if x["state"] == "sealed" or (x["state"] == "retry" and (x["next"] is None or x["next"] <= now)):
                if x["nbf"] is None or x["nbf"] <= now:
                    out.append(i)
        return sorted(out, key=lambda i: (self.b[i]["first"], i))


def test_model_randomised(make_store, clock):
    rnd = random.Random(20261023)
    st = make_store(cfg(), durable=False)
    m = Model()
    routes, keys, dests = ["r1", "r2"], ["a", "b", "c"], ["hermes:x", "script:y"]
    windows = [(0, 0, 3, 400), (10, 40, 4, 300), (30, 20, 50, 250), (5, 100, 2, 1000)]
    dd_pool = [f"d{i}" for i in range(60)]
    for step in range(10_000):
        op = rnd.random()
        now = clock.t
        if op < 0.45:
            route, key, dest = rnd.choice(routes), rnd.choice(keys), rnd.choice(dests) if rnd.random() < 0.1 \
                else "hermes:x"
            payload = {"p": "x" * rnd.randint(5, 120)}
            dd = rnd.choice(dd_pool) if rnd.random() < 0.3 else None
            w = rnd.choice(windows)
            r = st.enqueue("src", key, dest, payload, route=route, dedup_id=dd, quiet_s=w[0], max_s=w[1],
                           max_events=w[2], max_bytes=w[3])
            exp = m.enqueue(now, route, key, dest, size_of(payload), dd, w)
            assert (r.event_id, r.batch_id, r.duplicate, r.replayed) == exp, step
        elif op < 0.60:
            clock.advance(rnd.choice([0, 1, 3, 7, 15, 40]))
        elif op < 0.70:
            assert st.seal_ready(now) == m.seal_ready(now), step
        elif op < 0.85:
            c = [x["id"] for x in st.candidates(now)]
            assert c == m.candidates(now), step
            if c:
                bid = rnd.choice(c)
                owner = f"w{step}"
                assert st.claim(bid, 60, owner, now=now)
                m.b[bid].update(state="leased", owner=owner)
        elif op < 0.95:
            leased = [i for i, x in m.b.items() if x["state"] == "leased"]
            if leased:
                bid = rnd.choice(leased)
                owner = m.b[bid]["owner"]
                if rnd.random() < 0.05:
                    with pytest.raises(LeaseLost):
                        st.mark_delivered(bid, "not-the-owner", "ok", now=now)
                    continue
                k = rnd.random()
                if k < 0.5:
                    st.mark_delivered(bid, owner, "ok", now=now)
                    m.b[bid].update(state="delivered", attempts=m.b[bid]["attempts"] + 1)   # ok is counted
                elif k < 0.85:
                    nxt = now + rnd.choice([0, 5, 30])
                    counted = rnd.random() < 0.5
                    st.mark_retry(bid, owner, "e", "batch" if counted else "transport", nxt, counted, now=now)
                    m.b[bid].update(state="retry", next=nxt, attempts=m.b[bid]["attempts"] + int(counted))
                else:
                    st.mark_dead(bid, owner, "e", "permanent", now=now)
                    m.b[bid]["state"] = "dead"
        else:
            dead = [i for i, x in m.b.items() if x["state"] == "dead"]
            if dead and rnd.random() < 0.6:
                bid = rnd.choice(dead)
                assert st.replay(bid, now=now)
                m.b[bid].update(state="retry", next=now, nbf=now, attempts=0)
            else:
                cand = [i for i, x in m.b.items() if x["state"] in ("open", "sealed", "retry", "dead")]
                if cand:
                    bid = rnd.choice(cand)
                    assert st.drop(bid, now=now)
                    m.b[bid]["state"] = "dropped"
        if step % 250 == 0 or step == 9_999:
            db = {r["id"]: r for r in map(dict, st._read("SELECT * FROM batches"))}
            assert set(db) == set(m.b)
            for i, x in m.b.items():
                y = db[i]
                assert (y["state"], y["count"], y["bytes"], y["first_at"], y["last_at"], y["attempts"]) == \
                    (x["state"], x["count"], x["bytes"], x["first"], x["last"], x["attempts"]), (step, i)
                assert y["count"] <= x["me"] and (y["bytes"] <= x["mb"] or y["count"] == 1)
    consistent(st.path)
    assert len(m.b) > 500 and sum(1 for x in m.b.values() if x["state"] == "delivered") > 100
