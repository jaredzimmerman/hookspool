"""T9: "deliver never returns", against each real destination, through the real dispatcher
(T2 round 2; ARCHITECTURE §6.2 worker deadline, §14.2).

A worker thread cannot be aborted, so the dispatcher relies on every destination returning by
``ctx.deadline`` (``lease_until - 5 s``). Each receiver here never answers in a different way:
a socket that accepts and never writes, one that drips a byte at a time, a script that ignores
SIGTERM with a child holding its stdout, a script whose daemonised grandchild (its own session,
out of reach of the group kill) holds stdout, and secret, env and subscriptions paths that are
FIFOs (an ``open`` that blocks until a writer appears). In every case the attempt must end by
its deadline, the group must move on, other destinations must keep delivering meanwhile, and
the dispatcher must stop promptly.
"""
import io
import json
import os
import signal
import socket
import sys
import threading
import time

import pytest

from _httpstub import doc_for, encode, make_ctx
from conftest import SENTINEL, write_subs
from hookspool import config as C
from hookspool.destinations import build
from hookspool.dispatch import Dispatcher
from hookspool.store import Store

TIMEOUT_S = 2.0
GRACE_S = 5.0                 # lease = timeout + grace; deadline = lease - 5 = start + TIMEOUT_S
SLACK_S = 3.0                 # scheduling slack on a loaded CI runner; a hung destination would otherwise take for ever


class Unanswering:
    """A raw TCP receiver: ``/silent`` reads the request and never writes; ``/drip`` starts a
    response and then sends one byte every 0.2 s, forever."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.conns: list[socket.socket] = []
        self.hits: list[str] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self.stop.is_set():
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(c)
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c):
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = c.recv(65536)
                if not chunk:
                    return
                data += chunk
            path = data.split(b" ", 2)[1].decode()
            self.hits.append(path)
            if "drip" in path:
                c.sendall(b"HTTP/1.1 200 OK\r\nX-Drip: ")
                while not self.stop.is_set():
                    c.sendall(b"a")
                    time.sleep(0.2)
            else:
                self.stop.wait(120)
        except OSError:
            pass

    def close(self):
        self.stop.set()
        for c in self.conns:
            try:
                c.close()
            except OSError:
                pass
        self.sock.close()


HANG = r"""
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
sys.stdin.read()
child = subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                          " time.sleep(120)"])                       # same group, holds stdout
open(sys.argv[1], "a").write("%d %d\n" % (os.getpid(), child.pid))
time.sleep(120)
"""
ESCAPE = r"""
import os, subprocess, sys, time
sys.stdin.read()
gc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
open(sys.argv[1], "a").write("%d\n" % gc.pid)                       # its own session: the group kill misses it
if sys.argv[2] == "hang":
    time.sleep(120)
"""


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        pass
    import subprocess
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith("Z")


@pytest.fixture
def receiver():
    r = Unanswering()
    yield r
    r.close()


def test_every_real_destination_ends_by_its_deadline_through_the_dispatcher(tmp_path, receiver, subs_path):
    pidlog = tmp_path / "pids"
    hang, escape = tmp_path / "hang.py", tmp_path / "escape.py"
    hang.write_text(HANG)
    escape.write_text(ESCAPE)
    write_subs(subs_path, {"silent": {"secret": "test-secret-A"}, "drip": {"secret": "test-secret-A"}})
    base = f"http://127.0.0.1:{receiver.port}"
    secret = tmp_path / "audit.secret"
    secret.write_text("audit-secret-1\n")
    py = sys.executable
    dests = {
        "audit-silent": {"type": "http", "url": base + "/silent", "allow_insecure": True,
                         "secret": {"file": str(secret)}, "timeout_s": TIMEOUT_S},
        "audit-drip": {"type": "http", "url": base + "/drip", "allow_insecure": True,
                       "secret": {"file": str(secret)}, "timeout_s": TIMEOUT_S},
        "script:hang": {"argv": [py, str(hang), str(pidlog)], "timeout_s": TIMEOUT_S},
        "script:escape-hang": {"argv": [py, str(escape), str(pidlog), "hang"], "timeout_s": TIMEOUT_S},
        "script:escape-exit": {"argv": [py, str(escape), str(pidlog), "exit"], "timeout_s": TIMEOUT_S},
        "script:ok": {"argv": [py, "-c", "import sys; sys.stdin.read()"], "timeout_s": TIMEOUT_S},
    }
    cfg = C.merge(C.DEFAULTS, {
        "spool": {"home": str(tmp_path / "home")}, "alerts": {"dry": True},
        "dispatch": {"parallel": 10, "lease_grace_s": GRACE_S},
        "hermes": {"base_url": base, "subscriptions_path": str(subs_path), "timeout_s": TIMEOUT_S},
        "destinations": dests,
    })
    assert C.check(cfg, load_plugin=None).ok
    st = Store(str(tmp_path / "spool.db"), cfg)
    st.ensure_schema()
    hung = ["hermes:silent", "hermes:drip", "audit-silent", "audit-drip", "script:hang", "script:escape-hang"]
    ids, behind = {}, {}
    for name in hung + ["script:escape-exit"]:
        ids[name] = st.enqueue("src", "k", name, {"note": SENTINEL}, route=name, quiet_s=0, max_s=0).batch_id
        st.seal_ready()
        behind[name] = st.enqueue("src", "k", name, {"second": 1}, route=name, quiet_s=0, max_s=0).batch_id
        st.seal_ready()                                    # a second batch of the group, behind the head
    d = Dispatcher(st, cfg, jitter=lambda: 0.0, lock_path=str(tmp_path / "home" / "dispatch.lock"))
    d.start()
    try:
        t0 = time.monotonic()
        assert d.tick() == len(ids)                       # every head claimed at once, none behind it
        ok_id = st.enqueue("src", "fine", "script:ok", {"n": 1}, route="ok", quiet_s=0, max_s=0).batch_id
        st.seal_ready()
        d.tick()
        while time.monotonic() - t0 < TIMEOUT_S + 4 * SLACK_S and (d._work or st.batch(ok_id)["state"] != "delivered"):
            d._reap()
            time.sleep(0.05)
        took = time.monotonic() - t0
        assert not d._work, f"workers still running after {took:.1f}s: {[w.dest.name for w in d._work.values()]}"
        assert d._live == set() and all(v == 0 for v in d._inflight.values())
        assert st.batch(ok_id)["state"] == "delivered"     # other destinations kept delivering meanwhile
        for name in hung:
            b = st.batch(ids[name])
            assert (b["state"], b["last_class"]) == ("retry", "transport"), (name, b["state"], b["last_error"])
            (a,) = st.attempts(ids[name])
            assert a["outcome"] == "retry" and a["finished_at"] - a["started_at"] <= TIMEOUT_S + SLACK_S, (name, a)
            assert a["finished_at"] <= a["started_at"] + TIMEOUT_S + GRACE_S - 5 + SLACK_S   # by the deadline
        b = st.batch(ids["script:escape-exit"])
        assert b["state"] == "delivered"                   # it exited 0; the escaped grandchild cannot hold it
        # the hung heads are retried in their turn, the second batch of each group still waits behind them
        for name in hung:
            assert st.batch(behind[name])["state"] == "sealed"
        t1 = time.monotonic()
    finally:
        d.close()
        st.close()
    assert time.monotonic() - t1 < 3.0, "the dispatcher did not stop promptly"
    # the group kill reached everything in the script's process group
    pids = [int(x) for line in pidlog.read_text().splitlines() for x in line.split()]
    hang_pids = [int(x) for x in pidlog.read_text().splitlines()[0].split()] if pidlog.exists() else []
    end = time.monotonic() + 3
    while time.monotonic() < end and any(alive(p) for p in hang_pids):
        time.sleep(0.05)
    assert not any(alive(p) for p in hang_pids), "a process of the hung script's group survived"
    for p in pids:                                         # escaped grandchildren: ours to clean up
        try:
            os.kill(p, signal.SIGKILL)
        except OSError:
            pass


# ---- FIFOs where a regular file is expected -------------------------------------------------------------
@pytest.fixture
def fifo(tmp_path):
    p = tmp_path / "pipe"
    os.mkfifo(p)
    return p


def quick(fn):
    """Run fn in a thread and fail (rather than hang the suite) if it blocks."""
    box = {}

    def run():
        try:
            box["out"] = fn()
        except BaseException as e:      # re-raised in the test's thread
            box["err"] = e
    t = threading.Thread(target=run, daemon=True)
    t0 = time.monotonic()
    t.start()
    t.join(3.0)
    assert not t.is_alive(), "blocked on a FIFO"
    if "err" in box:
        raise box["err"]
    return box["out"], time.monotonic() - t0


def test_a_fifo_subscriptions_file_is_a_config_outcome_not_a_hang(make_config, fifo):
    cfg = make_config(hermes={"subscriptions_path": str(fifo)})
    d = build(cfg, "hermes:test-route")
    doc = doc_for()
    out, took = quick(lambda: d.deliver(doc, encode(doc), "hs_x", make_ctx(wall=True)))
    assert (out.kind, out.klass) == ("retry", "config") and took < 1.0


def test_a_fifo_secret_file_is_a_config_outcome_not_a_hang(make_config, fifo):
    cfg = make_config(destinations={"audit": {"type": "http", "url": "https://example.invalid/h",
                                              "secret": {"file": str(fifo)}}})
    d = build(cfg, "audit")
    doc = doc_for()
    out, took = quick(lambda: d.deliver(doc, encode(doc), "hs_x", make_ctx(wall=True)))
    assert (out.kind, out.klass) == ("retry", "config") and took < 1.0


def test_a_fifo_env_file_is_a_config_outcome_not_a_hang(make_config, fifo):
    cfg = make_config(destinations={"script:t": {"argv": [sys.executable, "-c", "pass"], "env_file": str(fifo)}})
    d = build(cfg, "script:t")
    doc = doc_for()
    out, took = quick(lambda: d.deliver(doc, encode(doc), "hs_x", make_ctx(wall=True)))
    assert (out.kind, out.klass) == ("retry", "config") and took < 1.0


@pytest.mark.parametrize("kind", ["file", "env_file"])
def test_a_fifo_secret_ref_fails_at_once_everywhere(fifo, kind):
    ref = {"file": str(fifo)} if kind == "file" else {"env_file": str(fifo), "key": "K"}
    with pytest.raises(C.SecretError):
        quick(lambda: C.resolve_secret(ref))
    rep, took = quick(lambda: C.check(C.merge(C.DEFAULTS, {"sources": {"s": {"module": "x", "action": "hermes:r",
                                                                             "secrets": {"secret": ref}}}}),
                                      load_plugin=None))
    assert not rep.ok and took < 1.0


def test_intake_answers_503_at_once_when_a_secret_is_a_fifo(make_config, make_store, fifo):
    """The intake side of the same hazard: verify reading a FIFO would hold a request thread
    (16 of them in all) for ever."""
    from hookspool.intake import Intake
    cfg = make_config(sources={"kaneo": {"module": "hookspool.sources.template", "scheme": "hmac-sha256-hex",
                                         "header": "X-Sig", "key": "id", "action": "hermes:r",
                                         "secrets": {"secret": {"file": str(fifo)}}}})
    ik = Intake(cfg, make_store(cfg))
    body = json.dumps({"id": "1", "note": SENTINEL}).encode()
    r, took = quick(lambda: ik.handle("POST", "/hooks/kaneo", [("Content-Length", str(len(body))), ("X-Sig", "00")],
                                      io.BytesIO(body), "127.0.0.1"))
    assert r.status == 503 and took < 1.0
