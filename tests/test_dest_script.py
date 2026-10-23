"""``script:<name>`` destination (C3, §6.6): stdin document, environment, exit codes, timeout
and deadline with process-group kill, stdout cap, emit parsing, and emit atomicity through the
dispatcher's fenced mark."""
import json
import os
import sys
import time

import pytest

from _httpstub import doc_for, encode, make_ctx
from conftest import SENTINEL
from hookspool.destinations import build
from hookspool.destinations.script import STDOUT_CAP, ScriptDestination, parse_emits

PY = sys.executable
NAME = "script:t"


def script(tmp_path, code: str, name: str = "s.py") -> str:
    p = tmp_path / name
    p.write_text(code)
    return str(p)


def dest(make_config, tmp_path, code=None, name=NAME, **spec):
    if code is not None:
        spec["argv"] = [PY, script(tmp_path, code)]
    cfg = make_config(destinations={name: spec})
    d = build(cfg, name)
    assert isinstance(d, ScriptDestination), d
    return d


def go(d, doc=None, delivery_id="hs_inst_7", ctx=None):
    doc = doc or doc_for()
    return d.deliver(doc, encode(doc), delivery_id, ctx or make_ctx(wall=True))


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:                                     # a zombie counts as dead
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        import subprocess
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(out) and not out.startswith("Z")


def wait_dead(pid: int, within: float = 3.0) -> bool:
    end = time.monotonic() + within
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


# ---- type properties --------------------------------------------------------------------------
def test_type_properties(make_config, tmp_path):
    d = dest(make_config, tmp_path, "pass")
    assert d.idempotency == "per-batch" and d.max_inflight == 1 and d.timeout_s == 300.0
    assert d.max_body_bytes is None
    d = dest(make_config, tmp_path, "pass", timeout_s=9, max_body_bytes=500, max_inflight=3)
    assert (d.timeout_s, d.max_body_bytes, d.max_inflight) == (9.0, 500, 3)


# ---- stdin and environment -----------------------------------------------------------------------
ECHO = r"""
import json, os, sys
doc = sys.stdin.buffer.read()
json.dump({"doc_len": len(doc), "doc": json.loads(doc)["count"],
           "batch": os.environ.get("HOOKSPOOL_BATCH_ID"), "delivery": os.environ.get("HOOKSPOOL_DELIVERY_ID"),
           "state": os.environ.get("HOOKSPOOL_STATE_DIR"), "cwd": os.getcwd(),
           "extra": os.environ.get("FROM_ENV_FILE"), "quoted": os.environ.get("QUOTED"),
           "ppid_sid": os.getsid(0) == os.getpid()},
          open(sys.argv[1], "w"))
"""


def test_stdin_is_the_exact_document_and_env_is_set(make_config, tmp_path):
    out_file = tmp_path / "out.json"
    envf = tmp_path / "x.env"
    envf.write_text("# c\nFROM_ENV_FILE=hello\nexport QUOTED='a b'\nHOOKSPOOL_BATCH_ID=forged\n")
    state = tmp_path / "state" / "t"
    d = dest(make_config, tmp_path, ECHO, env_file=str(envf), state_dir=str(state), cwd=str(tmp_path))
    d.argv.append(str(out_file))
    doc = doc_for(5)
    o = d.deliver(doc, encode(doc), "hs_inst_7", make_ctx(batch_id=7, wall=True))
    assert o.kind == "ok" and o.status == 0 and o.emits == []
    got = json.loads(out_file.read_text())
    assert got["doc_len"] == len(encode(doc)) and got["doc"] == 5
    assert got["batch"] == "7" and got["delivery"] == "hs_inst_7"          # env_file cannot forge ours
    assert got["extra"] == "hello" and got["quoted"] == "a b"
    assert got["state"] == str(state) and state.is_dir()
    assert os.path.realpath(got["cwd"]) == os.path.realpath(str(tmp_path))
    assert got["ppid_sid"] is True                                      # own session


def test_state_dir_env_absent_when_not_configured(make_config, tmp_path):
    out_file = tmp_path / "o.json"
    d = dest(make_config, tmp_path, ECHO)
    d.argv.append(str(out_file))
    go(d)
    assert json.loads(out_file.read_text())["state"] is None


def test_environment_is_minimal_a_parent_only_variable_is_absent(make_config, tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_PARENT_SECRET", "leak")
    monkeypatch.setenv("HOOKSPOOL_STATE_DIR", "/parent/state")
    out_file = tmp_path / "env.json"
    code = "import json, os, sys\njson.dump(dict(os.environ), open(sys.argv[1], 'w'))"
    d = dest(make_config, tmp_path, code)
    d.argv.append(str(out_file))
    assert go(d).kind == "ok"
    env = json.loads(out_file.read_text())
    assert "SENTINEL_PARENT_SECRET" not in env and "HOOKSPOOL_STATE_DIR" not in env
    assert "PATH" in env
    assert {k for k in env if not k.startswith(("LC_", "__CF"))} <= {
        "PATH", "HOME", "LANG", "HOOKSPOOL_BATCH_ID", "HOOKSPOOL_DELIVERY_ID", "PWD", "SHLVL", "_"}, sorted(env)


def test_undecodable_env_file_is_config(make_config, tmp_path):
    envf = tmp_path / "bad.env"
    envf.write_bytes(b"A=\xff\xfe\n")
    marker = tmp_path / "ran"
    d = dest(make_config, tmp_path, f"open({str(marker)!r}, 'w').close()", env_file=str(envf))
    o = go(d)
    assert (o.kind, o.klass) == ("retry", "config") and not marker.exists()
    assert any("env_file" in p for p in d.check())


def test_env_file_is_read_fresh_every_attempt(make_config, tmp_path):
    out_file = tmp_path / "o.json"
    envf = tmp_path / "x.env"
    envf.write_text("FROM_ENV_FILE=one\n")
    d = dest(make_config, tmp_path, ECHO, env_file=str(envf))
    d.argv.append(str(out_file))
    go(d)
    first = json.loads(out_file.read_text())["extra"]
    envf.write_text("FROM_ENV_FILE=two\n")
    go(d)
    assert (first, json.loads(out_file.read_text())["extra"]) == ("one", "two")


def test_unreadable_env_file_is_config_and_script_does_not_run(make_config, tmp_path):
    marker = tmp_path / "ran"
    d = dest(make_config, tmp_path, f"open({str(marker)!r}, 'w').close()", env_file=str(tmp_path / "missing.env"))
    o = go(d)
    assert (o.kind, o.klass) == ("retry", "config") and not marker.exists()


def test_unwritable_state_dir_is_config(make_config, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    d = dest(make_config, tmp_path, "pass", state_dir=str(blocker / "sub"))
    o = go(d)
    assert (o.kind, o.klass) == ("retry", "config")


def test_script_that_ignores_stdin_still_succeeds(make_config, tmp_path):
    d = dest(make_config, tmp_path, "pass")
    big = doc_for(2000)
    o = d.deliver(big, encode(big), "hs_i_1", make_ctx(wall=True))      # larger than a pipe buffer
    assert o.kind == "ok"


# ---- exit codes ------------------------------------------------------------------------------------
def test_nonzero_exit_is_a_counted_batch_failure_with_the_stderr_tail(make_config, tmp_path):
    d = dest(make_config, tmp_path, "import sys\nsys.stderr.write('boom ' * 200 + 'END')\nsys.exit(3)")
    o = go(d)
    assert (o.kind, o.klass, o.status, o.reason) == ("retry", "batch", 3, "exit 3")
    assert o.detail.startswith("exit 3: ") and o.detail.endswith("END") and len(o.detail) < 400


def test_exit_reason_never_carries_script_output(make_config, tmp_path):
    d = dest(make_config, tmp_path, f"import sys\nprint({SENTINEL!r})\nsys.exit(1)")
    o = go(d)
    assert SENTINEL not in o.reason


def test_killed_by_signal_is_a_batch_failure(make_config, tmp_path):
    d = dest(make_config, tmp_path, "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)")
    o = go(d)
    assert (o.kind, o.klass) == ("retry", "batch") and "signal 9" in o.reason


def test_cannot_start_is_transport(make_config, tmp_path):
    d = dest(make_config, tmp_path, argv=[str(tmp_path / "no-such-binary")])
    o = go(d)
    assert (o.kind, o.klass) == ("retry", "transport") and "cannot start" in o.reason
    d = dest(make_config, tmp_path, "pass", cwd=str(tmp_path / "no-such-dir"))
    assert go(d).klass == "transport"


def test_no_argv_is_config(make_config, tmp_path):
    d = dest(make_config, tmp_path, name=NAME)
    assert go(d).klass == "config" and d.check()


# ---- timeout, deadline, process group ---------------------------------------------------------------
HANG_WITH_CHILD = r"""
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(sys.argv[1], "w").write(str(child.pid) + " " + str(os.getpid()))
time.sleep(120)
"""


def pids(path):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if os.path.exists(path) and os.path.getsize(path):
            return [int(x) for x in open(path).read().split()]
        time.sleep(0.02)
    raise AssertionError("script never started")


def test_timeout_kills_the_whole_process_group(make_config, tmp_path):
    pidfile = tmp_path / "pids"
    d = dest(make_config, tmp_path, HANG_WITH_CHILD, timeout_s=1.0)
    d.argv.append(str(pidfile))
    t0 = time.monotonic()
    o = go(d)
    took = time.monotonic() - t0
    child, parent = pids(pidfile)
    assert (o.kind, o.klass, o.reason) == ("retry", "transport", "timeout after 1s") and took < 4
    assert wait_dead(parent) and wait_dead(child), "a process of the group survived the timeout"


def test_worker_deadline_kills_before_the_timeout(make_config, tmp_path):
    pidfile = tmp_path / "pids"
    d = dest(make_config, tmp_path, HANG_WITH_CHILD, timeout_s=300)
    d.argv.append(str(pidfile))
    t0 = time.monotonic()
    o = go(d, ctx=make_ctx(wall=True, ttl=1.0))
    child, parent = pids(pidfile)
    assert (o.klass, o.reason) == ("transport", "deadline") and time.monotonic() - t0 < 4
    assert wait_dead(parent) and wait_dead(child)


def test_deadline_already_passed_does_not_start_the_script(make_config, tmp_path):
    marker = tmp_path / "ran"
    d = dest(make_config, tmp_path, f"open({str(marker)!r}, 'w').close()")
    o = go(d, ctx=make_ctx(wall=True, ttl=-1))
    assert (o.klass, o.reason) == ("transport", "deadline") and not marker.exists()


def test_script_ignoring_sigterm_is_still_killed(make_config, tmp_path):
    code = "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(120)"
    d = dest(make_config, tmp_path, code, timeout_s=0.5)
    t0 = time.monotonic()
    assert go(d).klass == "transport" and time.monotonic() - t0 < 3


def test_a_detached_child_holding_stdout_does_not_hold_the_worker(make_config, tmp_path):
    pidfile = tmp_path / "pids"
    code = ("import subprocess, sys, os\n"
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "open(sys.argv[1], 'w').write(str(c.pid) + ' ' + str(os.getpid()))\n")
    d = dest(make_config, tmp_path, code, timeout_s=30)
    d.argv.append(str(pidfile))
    t0 = time.monotonic()
    o = go(d)
    child, _ = pids(pidfile)
    assert o.kind == "ok" and time.monotonic() - t0 < 6
    assert wait_dead(child, 4), "a descendant keeping our pipes open outlived the attempt"


# ---- stdout cap -------------------------------------------------------------------------------------
def test_stdout_over_the_cap_fails_the_attempt_instead_of_truncating(make_config, tmp_path):
    code = f"import sys\nsys.stdout.write('x' * {STDOUT_CAP + 10})\nsys.stdout.flush()"
    d = dest(make_config, tmp_path, code)
    o = go(d)
    assert (o.kind, o.klass) == ("retry", "batch") and "1 MiB" in o.reason and o.emits == []


def test_endless_output_is_bounded_and_ends_at_the_timeout(make_config, tmp_path):
    code = "import sys\nwhile True:\n    sys.stdout.write('y' * 65536)\n    sys.stdout.flush()"
    d = dest(make_config, tmp_path, code, timeout_s=0.7)
    t0 = time.monotonic()
    o = go(d)
    assert o.klass == "transport" and time.monotonic() - t0 < 4


def test_stdout_at_the_cap_is_fine(make_config, tmp_path):
    code = f"import sys\nsys.stdout.write('x' * {STDOUT_CAP})"
    o = go(dest(make_config, tmp_path, code))
    assert o.kind == "ok"


# ---- emits (parsing) ---------------------------------------------------------------------------------
EMITS = r"""
import json, sys
sys.stdin.read()
print("progress: working")
print(json.dumps({"enqueue": {"source": "gmail", "key": "a", "payload": {"n": 1}, "action": "hermes:test-route"}}))
print("   " + json.dumps({"enqueue": {"source": "gmail", "key": "b", "payload": {"n": 2}}}) + "  ")
print(json.dumps({"other": {"x": 1}}))
print("{not json}")
"""


def test_enqueue_lines_become_emits_and_other_lines_are_ignored(make_config, tmp_path):
    o = go(dest(make_config, tmp_path, EMITS))
    assert o.kind == "ok"
    assert [e["key"] for e in o.emits] == ["a", "b"]
    assert o.emits[0] == {"source": "gmail", "key": "a", "payload": {"n": 1}, "action": "hermes:test-route"}
    assert o.detail == "exit 0, 2 emitted"


def test_exit_zero_with_no_output_is_a_successful_noop(make_config, tmp_path):
    o = go(dest(make_config, tmp_path, "pass"))
    assert (o.kind, o.emits, o.status) == ("ok", [], 0)


@pytest.mark.parametrize("line", [
    '{"enqueue": 5}', '{"enqueue": {"source": "s", "key": "k"}}',                     # missing payload
    '{"enqueue": {"source": "s", "key": "k", "payload": {}, "surprise": 1}}',          # unknown key
    '{"enqueue": {"source": "s", "key": "k", "payload": {}',                           # truncated JSON
    '{ "enqueue": {"source": "s", "key": "k", "payload": {}}',                         # space, invalid JSON
    "{'enqueue': {'source': 's', 'key': 'k', 'payload': {}}}",                         # Python repr
])
def test_malformed_enqueue_is_a_counted_batch_failure(make_config, tmp_path, line):
    code = f"import sys\nsys.stdout.write({line + chr(10)!r})"
    o = go(dest(make_config, tmp_path, code))
    assert (o.kind, o.klass, o.reason) == ("retry", "batch", "bad emit") and o.emits == []


def test_non_utf8_in_an_emit_is_a_bad_emit_not_a_replacement_character(make_config, tmp_path):
    code = ("import sys\nsys.stdout.buffer.write(b'{\"enqueue\": {\"source\": \"s\", \"key\": \"k\","
            " \"payload\": \"a\\xffb\"}}\\n')")
    o = go(dest(make_config, tmp_path, code))
    assert (o.kind, o.klass, o.reason) == ("retry", "batch", "bad emit") and o.emits == []


def test_non_utf8_noise_outside_an_enqueue_is_ignored(make_config, tmp_path):
    code = "import sys\nsys.stdout.buffer.write(b'\\xff\\xfe noise\\n{\\xff\\n')"
    assert go(dest(make_config, tmp_path, code)).kind == "ok"


def test_failing_script_emits_nothing_even_if_it_printed_enqueues(make_config, tmp_path):
    code = EMITS + "\nsys.exit(2)"
    o = go(dest(make_config, tmp_path, code))
    assert (o.kind, o.klass) == ("retry", "batch") and o.emits == []


def test_parse_emits_unit():
    assert parse_emits(b"") == []
    assert parse_emits(b'noise\n{"enqueue": {"source": "s", "key": "k", "payload": 1}}\n')[0]["key"] == "k"
    with pytest.raises(ValueError):
        parse_emits(b'{"enqueue": []}')


# ---- emit atomicity through the dispatcher ----------------------------------------------------------
def emit_line(key, **kw):
    return json.dumps({"enqueue": {"source": "delta", "key": key, "payload": {"k": key}, **kw}})


def run_dispatched(rig, tmp_path, code, **spec):
    d_spec = {"argv": [PY, script(tmp_path, code)], **spec}
    r = rig({"destinations": {NAME: d_spec}}, destination_factory=build)
    a = r.enqueue(dest=NAME, n=1)
    r.d.drain_once()
    return r, a


def events_for(r, source="delta"):
    """Events the scripts emitted, as (batch key) rows, oldest first."""
    return [dict(x) for x in r.store._conn().execute(
        "SELECT e.id, b.key FROM events e JOIN batches b ON b.id = e.batch_id WHERE b.source=? ORDER BY e.id",
        (source,))]


def test_emits_are_enqueued_with_the_delivered_mark(rig, tmp_path):
    code = ("import sys, json\nsys.stdin.read()\n"
            f"print({emit_line('x', action='hermes:test-route')!r})\n"
            f"print({emit_line('y', action='hermes:test-route')!r})\n")
    r, a = run_dispatched(rig, tmp_path, code)
    assert r.batch(a.batch_id)["state"] == "delivered"
    assert [e["key"] for e in events_for(r)] == ["x", "y"]
    b = r.store.attempts(a.batch_id)[0]
    assert b["outcome"] == "ok"


def test_one_invalid_emit_rejects_all_of_them_and_the_batch_stays_unmarked(rig, tmp_path, clock):
    bad = json.dumps({"enqueue": {"source": "delta", "key": "bad", "payload": [1], "action": "hermes:test-route"}})
    code = ("import sys\nsys.stdin.read()\n"
            f"print({emit_line('good', action='hermes:test-route')!r})\n"
            f"print({bad!r})\n")
    r, a = run_dispatched(rig, tmp_path, code)
    assert events_for(r) == []                                  # the valid one was not committed either
    b = r.batch(a.batch_id)
    assert b["state"] == "retry" and b["last_class"] == "batch" and b["attempts"] == 1
    assert "bad emit" in (b["last_error"] or "")


def test_script_side_bad_emit_twice_is_permanent(rig, tmp_path, clock):
    code = "import sys\nsys.stdin.read()\nprint('{\"enqueue\": {\"source\": \"delta\", \"key\": \"k\"}}')\n"
    r, a = run_dispatched(rig, tmp_path, code)
    b = r.batch(a.batch_id)
    assert b["state"] == "retry" and b["attempts"] == 1 and "bad emit" in b["last_error"]
    clock.advance(10_000)
    r.d.drain_once()
    b = r.batch(a.batch_id)
    assert b["state"] == "dead" and "bad emit twice" in b["last_error"]


def test_a_crash_between_the_emits_and_the_commit_leaves_neither(rig, tmp_path, monkeypatch):
    from hookspool import store as store_mod
    code = ("import sys\nsys.stdin.read()\n"
            f"print({emit_line('x', action='hermes:test-route')!r})\n")

    def boom(point):
        if point == "deliver:after_emits":
            raise store_mod.SpoolError("simulated crash")
    monkeypatch.setattr(store_mod, "_test_hook", boom)
    r, a = run_dispatched(rig, tmp_path, code)
    monkeypatch.setattr(store_mod, "_test_hook", None)
    assert events_for(r) == []
    assert r.batch(a.batch_id)["state"] == "leased"        # unmarked; the lease expires and it runs again


def test_failing_script_leaves_no_events_and_a_rerun_emits_once(rig, tmp_path, clock):
    flag = tmp_path / "flag"
    code = ("import sys, os\nsys.stdin.read()\n"
            f"print({emit_line('once', action='hermes:test-route')!r})\n"
            f"if not os.path.exists({str(flag)!r}):\n    open({str(flag)!r}, 'w').close()\n    sys.exit(1)\n")
    r, a = run_dispatched(rig, tmp_path, code)
    assert events_for(r) == [] and r.batch(a.batch_id)["state"] == "retry"
    clock.advance(30)
    r.d.drain_once()
    assert r.batch(a.batch_id)["state"] == "delivered" and [e["key"] for e in events_for(r)] == ["once"]


def test_timeout_leaves_no_events(rig, tmp_path):
    code = ("import sys, time\nsys.stdin.read()\n"
            f"print({emit_line('never', action='hermes:test-route')!r}, flush=True)\n"
            "time.sleep(120)\n")
    r, a = run_dispatched(rig, tmp_path, code, timeout_s=1)
    assert events_for(r) == []
    b = r.batch(a.batch_id)
    assert b["state"] == "retry" and b["last_class"] == "transport"


def test_per_batch_delivery_id_reaches_the_script_on_every_attempt(rig, tmp_path, clock):
    log = tmp_path / "ids"
    code = ("import os, sys\nsys.stdin.read()\n"
            f"open({str(log)!r}, 'a').write(os.environ['HOOKSPOOL_DELIVERY_ID'] + '\\n')\nsys.exit(1)\n")
    r, a = run_dispatched(rig, tmp_path, code)
    clock.advance(30)
    r.d.drain_once()
    ids = log.read_text().split()
    assert ids == [f"hs_{r.store.instance_id()}_{a.batch_id}"] * 2


def test_dispatcher_hung_script_does_not_stall_its_group_past_the_lease(rig, tmp_path, clock):
    """The worker deadline ends a script that ignores its own timeout (timeout_s is longer
    than the lease would otherwise allow)."""
    pidfile = tmp_path / "pids"
    code = HANG_WITH_CHILD
    d_spec = {"argv": [PY, script(tmp_path, code), str(pidfile)], "timeout_s": 2}
    r = rig({"destinations": {NAME: d_spec}, "dispatch": {"lease_grace_s": 6}}, destination_factory=build)
    a = r.enqueue(dest=NAME)
    t0 = time.monotonic()
    r.d.drain_once()
    child, parent = pids(pidfile)
    assert time.monotonic() - t0 < 8
    assert wait_dead(parent) and wait_dead(child)
    b = r.batch(a.batch_id)
    assert b["state"] == "retry" and b["last_class"] == "transport"


# ---- check ---------------------------------------------------------------------------------------
def test_check(make_config, tmp_path):
    d = dest(make_config, tmp_path, "pass", state_dir=str(tmp_path / "new"), cwd=str(tmp_path))
    assert d.check() == []
    d = ScriptDestination(NAME, {"argv": ["no-such-binary-xyz"], "cwd": str(tmp_path / "nope"),
                                 "env_file": str(tmp_path / "nope.env"),
                                 "state_dir": str(tmp_path / "x" / "y" / "z")}, make_config())
    probs = " ".join(d.check())
    for word in ("PATH", "cwd", "env_file", "state_dir"):
        assert word in probs
    d = ScriptDestination(NAME, {"argv": [str(tmp_path / "not-exec")]}, make_config())
    (tmp_path / "not-exec").write_text("x")
    assert "not an executable" in " ".join(d.check())
