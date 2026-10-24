"""T9 crash matrix (ARCHITECTURE §12, §18 "crash safety").

Two matrices, both driven by ``kill -9`` at the store's ``_test_hook`` points:

1. **Store, exhaustive.** One child process runs a lifecycle that passes every hook point
   (joins, both seals, readiness seals, claim, every fenced mark with and without emits, lease
   recovery, replay, quarantine, reclassify, split, retention). A dry run counts how often each
   point fires; then the child is killed at *every* occurrence of *every* point. After each
   kill the spool must hold every acknowledged write and nothing half-done, and a dispatcher
   started on it must drain it completely.
2. **Dispatcher process.** A real dispatcher process delivering through real ``script:``
   destinations (whose emits feed a second script) is killed at the hook points it passes. A
   restarted dispatcher must deliver every batch at least once with the same content, and every
   batch's emits must be in the spool exactly once.
"""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from collections import Counter

import pytest

from conftest import SENTINEL, child_env
from hookspool import config as C
from hookspool.destinations.base import Destination, Outcome
from hookspool.dispatch import Dispatcher
from hookspool.store import Store, content_hash

T0 = 1_800_000_000.0
POINTS = ("enqueue:after_batch", "enqueue:after_event", "enqueue:after_seal", "enqueue:after_tee", "seal:mid", "claim:after_update",
          "deliver:after_emits", "mark:after_update", "recover:mid", "replay:mid", "quarantine:mid",
          "reclassify:mid", "split:mid", "prune:mid", "poll:after_enqueue")


def rows(path, q, args=()):
    c = sqlite3.connect(path)
    try:
        return c.execute(q, args).fetchall()
    finally:
        c.close()


def invariants(path):
    """What must hold after a crash at any line (stronger than test_store.consistent)."""
    assert rows(path, "PRAGMA integrity_check") == [("ok",)]
    assert rows(path, "PRAGMA foreign_key_check") == []
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    try:
        for b in c.execute("SELECT * FROM batches"):
            evs = c.execute("SELECT id, event_type, received_at, dedup_id, payload, size FROM events"
                            " WHERE batch_id=? AND excluded_at IS NULL ORDER BY id", (b["id"],)).fetchall()
            assert (b["count"], b["bytes"]) == (len(evs), sum(e["size"] for e in evs)), f"batch {b['id']} counters"
            if b["state"] not in ("open", "dropped") and b["destination"] != "quarantine":
                assert b["sealed_at"] is not None, f"batch {b['id']} ({b['state']}) not sealed"
                want = content_hash([json.loads(e["payload"]) for e in evs],
                                    [{"id": e["id"], "event_type": e["event_type"], "received_at": e["received_at"],
                                      "dedup_id": e["dedup_id"]} for e in evs])
                assert b["content_hash"] == want, f"batch {b['id']} content_hash does not match its events"
            if b["state"] == "open" and len(evs) > 1:
                assert b["count"] <= b["max_events"] and b["bytes"] <= b["max_bytes"], f"batch {b['id']} over bounds"
            if b["state"] == "leased":
                assert b["lease_owner"] and b["lease_until"], f"batch {b['id']} leased without a fence"
        assert c.execute("SELECT route, key FROM batches WHERE state='open' GROUP BY route, key"
                         " HAVING COUNT(*) > 1").fetchall() == []
        # an unfinished attempt exists only while its batch is leased (a crash mid-mark rolls back whole)
        assert c.execute("SELECT a.id FROM attempts a JOIN batches b ON b.id = a.batch_id"
                         " WHERE a.finished_at IS NULL AND b.state != 'leased'").fetchall() == []
        assert c.execute("SELECT batch_id FROM attempts WHERE finished_at IS NULL GROUP BY batch_id"
                         " HAVING COUNT(*) > 1").fetchall() == []
    finally:
        c.close()


class OkSink(Destination):
    """Accepts everything and records what it was given."""

    type_name = "stub"

    def __init__(self, name, config, seen):
        super().__init__(name, {"timeout_s": 5, "max_inflight": 4}, config)
        self.seen = seen

    def deliver(self, doc, body, delivery_id, ctx):
        self.seen.append((doc["batch_id"], doc["content_hash"], [m["id"] for m in doc["event_meta"]]))
        return Outcome.ok()


def drain_after_crash(path, now, cfg=None):
    """Restart a dispatcher on a crashed spool (fake clock far ahead) and deliver everything."""
    cfg = cfg or C.merge(C.DEFAULTS, {"alerts": {"dry": True},     # keep what is delivered, to check it
                                      "retention": {"delivered_days": 3650, "dropped_days": 3650}})
    seen: list = []
    st = Store(path, cfg, now=lambda: now)
    d = Dispatcher(st, cfg, now=lambda: now, jitter=lambda: 0.0, lock_path=os.path.join(os.path.dirname(path), "lk"),
                   destination_factory=lambda c, n: OkSink(n, c, seen))
    try:
        d.start()
        d.drain_once()
    finally:
        d.close()
        st.close()
    return seen


# ---- 1. the store, every occurrence of every hook point --------------------------------------------------
LIFECYCLE = r"""
import json, os, signal, sys
from collections import Counter
from hookspool import store, config
db, point, kill_on = sys.argv[1], sys.argv[2], int(sys.argv[3])
T = [1800000000.0]
st = store.Store(db, config.DEFAULTS, now=lambda: T[0])
seen = Counter()
def hook(p):
    seen[p] += 1
    if p == point and seen[p] == kill_on:
        os.kill(os.getpid(), signal.SIGKILL)
store._test_hook = hook
D = "hermes:r"
def ack(*a):
    print(json.dumps(a), flush=True)
def enq(key, i, **kw):
    kw.setdefault("quiet_s", 30); kw.setdefault("max_s", 120)
    r = st.enqueue("src", key, D, {"i": i, "pad": "x" * 40, "s": "PAYLOAD-SENTINEL-7f3a"}, dedup_id="e%d" % i, **kw)
    ack("enq", i, r.batch_id, r.duplicate)
# 1. enqueue: joins, a post-join seal (max_events), a pre-join seal (max_bytes), a duplicate
enq("a", 1); enq("a", 2); enq("b", 3, max_events=2); enq("b", 4, max_events=2)
enq("c", 5, max_bytes=120); enq("c", 6, max_bytes=120); enq("a", 7); enq("a", 1)
# 1b. one request copied to two tee routes: three event rows, all in one transaction
enq("t", 8, tees=[("tee-one", "hermes:t1"), ("tee-two", "script:t2")])
# 2. readiness seals, two per transaction
T[0] += 500
ack("sealed", st.seal_ready(T[0], chunk=2))
# 3. every head: one delivered with an emit, one retried, one dead, the rest delivered
n, retry_bid, dead_bid = 0, None, None
while True:
    cands = st.candidates(T[0])
    if not cands:
        break
    bid = cands[0]["id"]; owner = "w%d" % n
    assert st.claim(bid, 60, owner, now=T[0])
    a = st.start_attempt(bid, owner, now=T[0])
    if n == 0:
        st.mark_delivered(bid, owner, "ok", emits=[{"source": "src", "key": "z", "action": D, "payload": {"emit": 1},
                                                   "dedup_id": "emit-1", "quiet_s": 0, "max_s": 0}], attempt_id=a.id)
        ack("delivered", bid, True)
    elif n == 1:
        st.mark_retry(bid, owner, "boom", "batch", T[0] + 10000, True, attempt_id=a.id)
        retry_bid = bid; ack("retry", bid)
    elif n == 2:
        st.mark_dead(bid, owner, "rejected", "permanent", attempt_id=a.id)
        dead_bid = bid; ack("dead", bid)
    else:
        st.mark_delivered(bid, owner, "ok", attempt_id=a.id); ack("delivered", bid, False)
    n += 1
# 4. a lease whose worker died, recovered at dispatcher startup
st.retry_now(retry_bid, now=T[0])
assert st.claim(retry_bid, 60, "dead-worker", now=T[0]); st.start_attempt(retry_bid, "dead-worker", now=T[0])
ack("recovered", st.recover_leases(T[0], all_leases=True))
# 5. replay of the dead batch
ack("replayed", dead_bid, st.replay(dead_bid, now=T[0]))
# 6. quarantine, then reclassify it
eid, qb = st.quarantine("src", b'{"q": 1}', {"X-Kept": "1", "X-Sig": "s"}, "plugin error: KeyError in key",
                        auth_headers=("X-Sig",))
ack("quarantined", qb)
st.reclassify_quarantine(qb, lambda s, raw, h: {"key": "q", "action": D, "payload": {"q": 1}, "dedup_id": "q-1",
                                                "quiet_s": 0, "max_s": 0}, now=T[0])
ack("reclassified", qb)
# 7. split a sealed batch of four
for i in range(10, 14):
    enq("s", i, quiet_s=0, max_s=0)
sb = [b for b in st.list("open", limit=100) if b["key"] == "s"][0]["id"]
st.seal_ready(T[0])
ack("split", sb, st.split(sb, max_events=1, now=T[0]))
# 7b. the poll clock: a new instance is scheduled, then its due tick is enqueued with next_at in one transaction
st.poll_tick("pl", D, 600, 0, now=T[0])
ack("polled", st.poll_tick("pl", D, 600, 0, now=T[0]).action)
# 8. retention, 30 days later, one row per transaction
T[0] += 30 * 86400
ack("pre_prune", sorted(b["id"] for b in st.list(limit=1000) if b["state"] not in ("delivered", "dropped")))
ack("pruned", st.prune(chunk=1))
ack("counts", dict(seen))
"""


def run_lifecycle(db, point="", kill_on=0):
    p = subprocess.run([sys.executable, "-c", LIFECYCLE, db, point, str(kill_on)], env=child_env(),
                       capture_output=True, text=True, timeout=60)
    out = [json.loads(line) for line in p.stdout.splitlines()]
    return p.returncode, out, p.stderr


_COUNTS: dict | None = None


def lifecycle_counts() -> dict:
    """How often each hook point fires in an uninterrupted lifecycle (a dry run, once)."""
    global _COUNTS
    if _COUNTS is None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "spool.db")
            Store(db, C.DEFAULTS).ensure_schema()
            rc, out, err = run_lifecycle(db)
            assert rc == 0, err
            _COUNTS = out[-1][1]
    return _COUNTS


def pytest_generate_tests(metafunc):
    if metafunc.function.__name__ == "test_kill9_at_every_occurrence_of_every_hook":
        counts = lifecycle_counts()
        cases = [(p, k) for p in POINTS for k in range(1, int(counts.get(p, 0)) + 1)]
        metafunc.parametrize("point,k", cases, ids=[f"{p}#{k}" for p, k in cases])


def test_the_lifecycle_passes_every_hook_point():
    counts = lifecycle_counts()
    assert set(POINTS) <= set(counts), f"the lifecycle misses hook points: {set(POINTS) - set(counts)}"
    assert sum(counts.values()) >= 50


def test_every_hook_point_in_the_store_is_in_the_matrix():
    """A hook point added to store.py without being added here would escape the matrix."""
    import re
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hookspool", "store.py")).read()
    assert set(re.findall(r'_hook\("([a-z:_]+)"\)', src)) == set(POINTS)


def test_kill9_at_every_occurrence_of_every_hook(tmp_path, point, k):
    db = str(tmp_path / "spool.db")
    Store(db, C.DEFAULTS).ensure_schema()
    rc, out, err = run_lifecycle(db, point, k)
    assert rc == -signal.SIGKILL, (rc, err)
    assert SENTINEL not in err
    acks = Counter(a[0] for a in out)
    invariants(db)

    # every acknowledged write is there; nothing unacknowledged is half there
    pruning = acks["pre_prune"] > 0
    stored = {int(d[1:]) for (d,) in rows(db, "SELECT dedup_id FROM events WHERE dedup_id GLOB 'e[0-9]*'")}
    acked = {a[1] for a in out if a[0] == "enq"}
    if not pruning:
        assert acked <= stored, f"acked events lost: {sorted(acked - stored)}"
        assert stored <= acked, "an event whose enqueue never returned is stored"   # every kill is inside a txn
    if not pruning:
        copies = rows(db, "SELECT COUNT(*) FROM events WHERE dedup_id='e8'")[0][0]
        assert copies == (3 if 8 in acked else 0), f"a tee request left {copies} of its 3 copies"
    emit = rows(db, "SELECT COUNT(*) FROM events WHERE dedup_id='emit-1'")[0][0]
    assert emit <= 1
    if any(a[0] == "delivered" and a[2] for a in out) and not pruning:
        assert emit == 1, "a delivered mark committed without its emit"
    q = rows(db, "SELECT state FROM batches WHERE destination='quarantine'")
    if acks["reclassified"]:
        assert q in ([("delivered",)], []) and rows(db, "SELECT COUNT(*) FROM events WHERE dedup_id='q-1'")[0][0] <= 1
    elif acks["quarantined"]:
        assert q == [("dead",)] and rows(db, "SELECT COUNT(*) FROM events WHERE dedup_id='q-1'") == [(0,)]
        hdrs = json.loads(rows(db, "SELECT headers FROM events WHERE batch_id=(SELECT id FROM batches WHERE"
                                   " destination='quarantine')")[0][0])
        assert "X-Sig" not in hdrs and hdrs.get("X-Kept") == "1"
    else:
        assert q == []
    split = [a for a in out if a[0] == "split"]
    if split and not pruning:
        sb, parts = split[0][1], split[0][2]
        assert rows(db, "SELECT state FROM batches WHERE id=?", (sb,)) == [("dropped",)] and len(parts) == 4
    ticks = rows(db, "SELECT COUNT(*) FROM events WHERE dedup_id GLOB 'poll:pl:*'")[0][0]
    if acks["polled"] and not pruning:
        assert ticks == 1 and rows(db, "SELECT next_at FROM polls WHERE instance='pl'")[0][0] > T0 + 500
    elif not pruning:
        assert ticks == 0, "a tick was committed without its next_at"
    pre = [a[1] for a in out if a[0] == "pre_prune"]
    if pre:
        left = {r[0] for r in rows(db, "SELECT id FROM batches")}
        assert set(pre[0]) <= left, "retention removed a batch that was not delivered or dropped"

    # a dispatcher started on the crashed spool drains it completely, content unchanged
    before = {r[0]: r[1] for r in rows(db, "SELECT id, content_hash FROM batches WHERE content_hash IS NOT NULL")}
    seen = drain_after_crash(db, T0 + 40 * 86400)
    invariants(db)
    pending = rows(db, "SELECT id, state FROM batches WHERE state IN ('open', 'sealed', 'leased', 'retry')")
    assert pending == [], pending
    for bid, h, _ in seen:
        if bid in before:
            assert h == before[bid], f"batch {bid} was delivered with different content after the crash"
    assert len({bid for bid, _, _ in seen}) == len(seen), "a batch was delivered twice by one dispatcher run"
    if not pruning:
        for i in acked:
            st = rows(db, "SELECT b.state FROM events e JOIN batches b ON b.id = e.batch_id WHERE e.dedup_id=?",
                      (f"e{i}",))
            assert st and st[0][0] in ("delivered", "dead"), (i, st)


# ---- 2. the dispatcher process, through real script destinations ------------------------------------------------
WORK = r"""
import json, os, sys
doc = json.load(sys.stdin)
with open(sys.argv[1], "a") as f:
    f.write(json.dumps({"batch": doc["batch_id"], "hash": doc["content_hash"],
                        "events": [m["id"] for m in doc["event_meta"]]}) + "\n")
print(json.dumps({"enqueue": {"source": "src", "key": "out", "action": "script:sink",
                              "payload": {"from": doc["batch_id"]}, "quiet_s": 0, "max_s": 0}}))
"""
SINK = r"""
import json, sys
doc = json.load(sys.stdin)
with open(sys.argv[1], "a") as f:
    f.write(json.dumps({"sink": doc["batch_id"], "from": [e["from"] for e in doc["events"]]}) + "\n")
"""
DISPATCH_CHILD = r"""
import json, os, signal, sys
from collections import Counter
from hookspool import store, config
from hookspool.dispatch import Dispatcher
cfg_path, point, kill_on = sys.argv[1], sys.argv[2], int(sys.argv[3])
cfg = config.load_config(cfg_path)
seen = Counter()
def hook(p):
    seen[p] += 1
    if p == point and seen[p] == kill_on:
        os.kill(os.getpid(), signal.SIGKILL)
store._test_hook = hook
st = store.Store(config.db_path(cfg), cfg)
d = Dispatcher(st, cfg, config_path=cfg_path, jitter=lambda: 0.0)
d.start()
d.drain_once()
d.close()
print(json.dumps(dict(seen)), flush=True)
"""
DISPATCH_POINTS = ("claim:after_update", "mark:after_update", "deliver:after_emits", "enqueue:after_batch",
                   "enqueue:after_event", "seal:mid")


def dispatch_setup(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    work, sink = tmp_path / "work.py", tmp_path / "sink.py"
    work.write_text(WORK)
    sink.write_text(SINK)
    wlog, slog = tmp_path / "work.log", tmp_path / "sink.log"
    cfg_path = home / "config.toml"
    cfg_path.write_text(f"""
[spool]
home = "{home}"
[alerts]
dry = true
[dispatch]
parallel = 3
[destinations."script:work"]
argv = ["{sys.executable}", "{work}", "{wlog}"]
timeout_s = 20
max_inflight = 3
[destinations."script:sink"]
argv = ["{sys.executable}", "{sink}", "{slog}"]
timeout_s = 20
""")
    cfg = C.load_config(str(cfg_path))
    st = Store(C.db_path(cfg), cfg)
    st.ensure_schema()
    for i in range(6):
        st.enqueue("src", f"k{i % 3}", "script:work", {"i": i, "note": SENTINEL}, quiet_s=0, max_s=0)
        st.seal_ready()                     # one batch per event, three groups of two
    st.close()
    return cfg, str(cfg_path), wlog, slog


def run_dispatch_child(cfg_path, point="", k=0):
    return subprocess.run([sys.executable, "-c", DISPATCH_CHILD, cfg_path, point, str(k)], env=child_env(),
                          capture_output=True, text=True, timeout=120)


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture(scope="module")
def dispatch_counts(tmp_path_factory):
    cfg, cfg_path, wlog, slog = dispatch_setup(tmp_path_factory.mktemp("dry"))
    p = run_dispatch_child(cfg_path)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.splitlines()[-1])


@pytest.mark.parametrize("point,k", [(p, k) for p in DISPATCH_POINTS for k in (1, 2, 5)],
                         ids=[f"{p}#{k}" for p in DISPATCH_POINTS for k in (1, 2, 5)])
def test_dispatcher_killed_at_a_hook_redelivers_and_emits_exactly_once(tmp_path, dispatch_counts, point, k):
    if k > dispatch_counts.get(point, 0):
        pytest.skip(f"{point} fires {dispatch_counts.get(point, 0)} time(s) in a dispatcher run")
    cfg, cfg_path, wlog, slog = dispatch_setup(tmp_path)
    p = run_dispatch_child(cfg_path, point, k)
    if p.returncode == 0 and point == "seal:mid":
        pytest.skip("seal:mid fired fewer times in this run (it depends on when the emits were sealed)")
    assert p.returncode == -signal.SIGKILL, (p.returncode, p.stderr)
    assert SENTINEL not in p.stderr
    db = C.db_path(cfg)
    invariants(db)
    time.sleep(0.3)                                   # a script the killed dispatcher started may still finish
    st = Store(db, cfg)
    d = Dispatcher(st, cfg, config_path=cfg_path, jitter=lambda: 0.0)
    try:
        d.start()                                     # the flock proves every lease is the dead process's
        d.drain_once()
    finally:
        d.close()
        st.close()
    invariants(db)
    assert rows(db, "SELECT COUNT(*) FROM batches WHERE state IN ('open', 'sealed', 'leased', 'retry')") == [(0,)]
    assert rows(db, "SELECT COUNT(*) FROM batches WHERE state='dead'") == [(0,)]
    originals = [r[0] for r in rows(db, "SELECT id FROM batches WHERE destination='script:work' ORDER BY id")]
    assert len(originals) == 6
    work = lines(wlog)
    by_batch: dict = {}
    for w in work:
        by_batch.setdefault(w["batch"], set()).add((w["hash"], tuple(w["events"])))
    assert set(by_batch) == set(originals), "a batch was never delivered"
    assert all(len(v) == 1 for v in by_batch.values()), "a batch was redelivered with different content"
    # each delivered batch's emit is in the spool exactly once (emits commit with the mark)
    froms = [json.loads(r[0])["from"] for r in rows(db, "SELECT payload FROM events WHERE source='src' AND"
                                                       " batch_id IN (SELECT id FROM batches WHERE destination="
                                                       "'script:sink')")]
    assert sorted(froms) == sorted(originals), "an emit was lost or duplicated"
    sunk = {f for s in lines(slog) for f in s["from"]}
    assert sunk == set(originals), "an emitted event never reached its destination"
