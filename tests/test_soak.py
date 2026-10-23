"""T9 soak (ARCHITECTURE §18 "concurrency" and "soak"; §4.1, §5, §6.1).

Phase 1, gate closed: writer processes enqueue random bursts (random keys, sizes and
duplicate redeliveries; one writer is killed with SIGKILL mid-run) while the destination is
paused and the dispatcher keeps polling. No attempt may be made, every batch must seal, and
no batch may exceed ``max_events`` or ``max_bytes`` however long the gate stays closed.

Phase 2, drain: the destination is resumed and fails at random (transport, throttle, counted
batch failures), and the dispatcher is abandoned mid-run without recording anything (as if it
was killed after the receiver's 200) and replaced. Every acknowledged event must be delivered,
in exactly one batch, every attempt of a batch must carry the same events and ``content_hash``,
and each group must be delivered in FIFO order.

``HOOKSPOOL_SOAK_EVENTS`` (per writer, default 250) scales the run.
"""
import json
import os
import random
import signal
import subprocess
import sys
import time

from conftest import SENTINEL, child_env
from hookspool import config as C
from hookspool.destinations.base import Destination, Outcome
from hookspool.dispatch import Dispatcher
from hookspool.store import Store

DEST = "hermes:soak"
MAX_EVENTS, MAX_BYTES = 25, 20_000
WRITER = r"""
import json, random, sys
from hookspool.store import Store
from hookspool.config import DEFAULTS
db, w, n, seed = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
rnd = random.Random(seed)
st = Store(db, DEFAULTS, durable=False)
sent = []
for i in range(n):
    if sent and rnd.random() < 0.08:
        did = rnd.choice(sent)                      # a provider redelivery
    else:
        did = "w%d-%d" % (w, i)
        sent.append(did)
    key = "k%d" % rnd.randrange(6)
    big = rnd.random() < 0.3                         # some bursts hit max_bytes first, the rest max_events
    r = st.enqueue("soak", key, "hermes:soak", {"w": w, "i": i, "pad": "x" * rnd.randrange(20, 4000 if big else 600),
                                               "note": "PAYLOAD-SENTINEL-7f3a"}, dedup_id=did,
                   quiet_s=rnd.choice([0.5, 1.0]), max_s=2.0, max_events=25, max_bytes=20000)
    print(json.dumps([did, r.event_id, r.duplicate]), flush=True)
"""


class Flaky(Destination):
    type_name = "stub"

    def __init__(self, name, config, rnd, log):
        super().__init__(name, {"timeout_s": 30, "max_inflight": 3}, config)
        self.rnd, self.log = rnd, log

    def deliver(self, doc, body, delivery_id, ctx):
        x = self.rnd.random()
        if x < 0.25:
            out = Outcome.transport("refused")
        elif x < 0.32:
            out = Outcome.throttle("http 429", status=429, retry_after_s=5)
        elif x < 0.37:
            out = Outcome.batch("exit 1", status=1)
        else:
            out = Outcome.ok()
        self.log.append({"batch": doc["batch_id"], "key": doc["key"], "hash": doc["content_hash"],
                         "events": [m["id"] for m in doc["event_meta"]], "ok": out.kind == "ok"})
        return out


def test_soak_bounds_hold_with_the_gate_closed_then_everything_drains(tmp_path):
    per = int(os.environ.get("HOOKSPOOL_SOAK_EVENTS", "250"))
    rnd = random.Random(20261023)
    cfg = C.merge(C.DEFAULTS, {"spool": {"home": str(tmp_path / "home")}, "alerts": {"dry": True},
                               "dispatch": {"parallel": 4}})
    db = str(tmp_path / "spool.db")
    st = Store(db, cfg)
    st.ensure_schema()
    log: list = []
    clock = {"off": 0.0}

    def wall():
        return time.time() + clock["off"]

    def dispatcher():
        d = Dispatcher(Store(db, cfg, now=wall), cfg, now=wall, jitter=lambda: 0.0,
                       destination_factory=lambda c, n: Flaky(n, c, rnd, log))
        d.start(take_lock=False)
        return d

    # ---- phase 1: the gate is closed while writers run ---------------------------------------------
    st.destination_set(DEST, state="paused", paused_by="soak")
    d = dispatcher()
    writers = [subprocess.Popen([sys.executable, "-c", WRITER, db, str(w), str(per), str(w + 7)], env=child_env(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for w in range(4)]
    killed = writers[3]
    acked: dict[str, int] = {}
    import threading

    def read(p):
        for line in p.stdout:
            did, eid, dup = json.loads(line)
            acked.setdefault(did, eid)
            if p is killed and len([k for k in acked if k.startswith("w3-")]) >= per // 3:
                p.send_signal(signal.SIGKILL)
                return
    readers = [threading.Thread(target=read, args=(p,), daemon=True) for p in writers]
    for t in readers:
        t.start()
    while any(p.poll() is None for p in writers):
        d.tick()                                         # seals ready batches; the gate claims nothing
        time.sleep(0.25)                                 # long enough between polls that size must seal
    for t in readers:
        t.join(10)
    errs = [p.stderr.read() for p in writers]
    assert all(p.returncode == 0 for p in writers[:3]), errs
    assert killed.returncode == -signal.SIGKILL
    assert all(SENTINEL not in e for e in errs)
    time.sleep(2.1)                                      # every window closes
    d.tick()
    rows = st._read("SELECT id, state, count, bytes, max_events, max_bytes FROM batches")
    assert log == [] and st._read("SELECT COUNT(*) FROM attempts")[0][0] == 0, "an attempt was made while paused"
    assert all(r["state"] == "sealed" for r in rows), "a batch is still open after its window closed"
    assert all(r["count"] <= MAX_EVENTS and r["bytes"] <= MAX_BYTES for r in rows), "a batch outgrew its bounds"
    sizes = st._read("SELECT COUNT(*) FROM batches WHERE count = ?", (MAX_EVENTS,))[0][0]
    assert sizes > 0, "the run never filled a batch to max_events; it does not test the bound"
    assert len(rows) >= (3 * per) // MAX_EVENTS
    stored = {r["dedup_id"]: r["id"] for r in st._read("SELECT id, dedup_id FROM events")}
    assert set(acked) <= set(stored), "an acknowledged event is missing"
    assert len(stored) == len(set(stored.values()))      # one event per dedup id: redeliveries were duplicates

    # ---- phase 2: resume, a flaky receiver, a dispatcher that dies holding leases -----------------------------
    st.destination_set(DEST, state="closed", consecutive_failures=0, paused_at=None, paused_by=None)
    abandoned = False
    for step in range(4000):
        d._reap()
        d.tick()
        if d._work:
            from concurrent.futures import FIRST_COMPLETED, wait
            wait(list(d._work), timeout=5, return_when=FIRST_COMPLETED)
        if not abandoned and len(log) >= 40:
            # die without recording: the futures are dropped, their batches stay leased
            for f in list(d._work):
                f.result(5)
            d._work.clear()
            d._pool.shutdown(wait=True)
            d._alert_pool.shutdown(wait=True)
            d = dispatcher()                             # startup recovery: every lease is a dead process's
            abandoned = True
        clock["off"] += 20.0                             # backoff, cooldown and Retry-After elapse
        left = st._read("SELECT COUNT(*) FROM batches WHERE state IN ('open', 'sealed', 'leased', 'retry')")[0][0]
        if left == 0 and not d._work:
            break
    d.close()
    assert abandoned
    assert [tuple(r) for r in st._read("SELECT state, COUNT(*) FROM batches GROUP BY state")] == [("delivered", len(rows))], \
        "every batch is delivered (nothing dead, dropped or pending)"
    # every attempt of a batch carried the same events and hash; each event in exactly one batch
    content: dict = {}
    for e in log:
        content.setdefault(e["batch"], set()).add((e["hash"], tuple(e["events"])))
    assert all(len(v) == 1 for v in content.values()), "a batch changed between attempts"
    delivered_events = [i for v in content.values() for _, evs in v for i in evs]
    assert sorted(delivered_events) == sorted(stored.values()), "an event was lost or delivered in two batches"
    for evs in (list(next(iter(v))[1]) for v in content.values()):
        assert evs == sorted(evs)
    # FIFO per group: the first successful delivery of each batch follows (first_at, id) order
    order = {r["id"]: (r["first_at"], r["id"]) for r in st._read("SELECT id, first_at FROM batches")}
    first_ok: dict[str, list] = {}
    seen_ok = set()
    for e in log:
        if e["ok"] and e["batch"] not in seen_ok:
            seen_ok.add(e["batch"])
            first_ok.setdefault(e["key"], []).append(e["batch"])
    for key, bids in first_ok.items():
        assert bids == sorted(bids, key=lambda b: order[b]), f"group {key} delivered out of order"
    redelivered = [b for b, v in content.items() if sum(1 for e in log if e["batch"] == b and e["ok"]) > 1]
    assert redelivered, "the abandoned dispatcher's batches were redelivered (at-least-once)"
    st.close()
