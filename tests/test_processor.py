"""The processor runtime (ARCHITECTURE 14.3.4): ``hookspool.processor``.

A throwaway package (``service.toml`` + ``processor.py``) is written per test; the runtime is driven
in-process through ``run()`` and, for the stdout and exit-code guarantees, as the real
``python -m hookspool.processor <instance>`` subprocess.
"""
import fcntl
import importlib
import io
import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

from conftest import SENTINEL
from hookspool import processor as P
from hookspool.destinations.script import parse_emits

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "deploy", "scripts")

MANIFEST = """\
name = "demo"
version = "1"
summary = "test package"

[source]
module = "source"

[settings.tz]
description = "time zone"
type = "string"
default = "UTC"

[settings.limit]
description = "how many"
type = "int"
default = 3

[settings.loud]
description = "a flag"
type = "bool"
default = false

[settings.who]
description = "people"
type = "list"
default = []

[processor]
module = "processor"
state = true

[outputs.alert]
description = "something happened"
"""


def doc(*evs, key="k1", bid=7):
    return {"source": "demo", "key": key, "batch_id": bid, "delivery_id": f"hs_demo_{bid}", "events": list(evs)}


class Rig:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.pkg = tmp_path / "demo"
        self.pkg.mkdir()
        (self.pkg / "service.toml").write_text(MANIFEST)
        self.state = tmp_path / "state"
        self.env = {"HOOKSPOOL_SERVICE_PACKAGE": "demo", "HOOKSPOOL_SERVICE_INSTANCE": "demo-a",
                    "HOOKSPOOL_SERVICE_DIR": str(self.pkg), "HOOKSPOOL_STATE_DIR": str(self.state),
                    "HOOKSPOOL_BATCH_ID": "7", "HOOKSPOOL_DELIVERY_ID": "hs_demo_7",
                    "HOOKSPOOL_DELTA_ROUTES": json.dumps({"alert": "hermes:alerts"}),
                    "HOOKSPOOL_SETTINGS": json.dumps({"tz": "Europe/Paris"}), "HOOKSPOOL_SECRET_REFS": "{}"}

    def processor(self, body: str) -> None:
        (self.pkg / "processor.py").write_text(textwrap.dedent(body))

    def run(self, d=None, **kw):
        out, err = io.StringIO(), io.StringIO()
        env = dict(self.env, **kw.pop("env", {}))
        rc = P.run("demo-a", d if d is not None else doc({"n": 1}), env=env, out=out, err=err, **kw)
        return rc, [json.loads(line)["enqueue"] for line in out.getvalue().splitlines()], err.getvalue()

    def journal(self, bid="7"):
        return P.load_journal(str(self.state), bid)

    def subprocess(self, d=None, env=None):
        e = {"PATH": os.environ["PATH"], "PYTHONPATH": ROOT, **self.env, **(env or {})}
        return subprocess.run([sys.executable, "-m", "hookspool.processor", "demo-a"], cwd=str(self.tmp),
                              input=json.dumps(d if d is not None else doc({"n": 1})), capture_output=True,
                              text=True, env=e, timeout=60)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


COUNTER = """\
def process(batch, ctx):
    n = ctx.load_state("counter", {"n": 0})["n"] + len(batch.events)
    ctx.save_state("counter", {"n": n})
    assert ctx.load_state("counter") == {"n": n}, "a pass reads its own staged write"
    return [{"event_type": "alert", "n": n, "tz": ctx.settings["tz"]}]
"""


# ---- the happy path, emits and state -------------------------------------------------------------------------------
def test_items_become_emits_and_state_is_applied_after_the_journal(rig):
    rig.processor(COUNTER)
    rc, emits, err = rig.run(doc({"a": 1}, {"b": 2}))
    assert rc == 0
    assert emits == [{"source": "demo-a", "key": "k1", "action": "hermes:alerts", "route": "alerts",
                      "payload": {"event_type": "alert", "n": 2, "tz": "Europe/Paris"}, "event_type": "alert",
                      "dedup_id": "hs_demo_7:0", "quiet_s": 0, "max_s": 0}]
    assert json.loads((rig.state / "counter.json").read_text()) == {"n": 2}
    j = rig.journal()
    assert j["applied"] is True and j["emits"] == emits
    assert (rig.state / "journal" / "7.json").stat().st_mode & 0o077 == 0


def test_the_item_key_wins_over_the_batch_key_and_dedup_ids_follow_the_item_index(rig):
    rig.processor("""\
        def process(batch, ctx):
            return [{"event_type": "nope"}, {"event_type": "alert", "key": "own"}, {"event_type": "alert"}]
    """)
    rc, emits, err = rig.run()
    assert rc == 0
    assert [(e["key"], e["dedup_id"]) for e in emits] == [("own", "hs_demo_7:1"), ("k1", "hs_demo_7:2")]


def test_the_batch_carries_ids_source_key_and_events(rig):
    rig.processor("""\
        def process(batch, ctx):
            return [{"event_type": "alert", "id": batch.id, "did": batch.delivery_id, "source": batch.source,
                     "key": batch.key, "events": batch.events, "count": batch.doc.get("count")}]
    """)
    rc, emits, _ = rig.run(dict(doc({"x": 1}, "skip", {"y": 2}), count=3))
    assert rc == 0
    assert emits[0]["payload"] == {"event_type": "alert", "id": "7", "did": "hs_demo_7", "source": "demo",
                                   "key": "k1", "events": [{"x": 1}, {"y": 2}], "count": 3}


def test_an_empty_result_is_a_successful_no_op(rig):
    rig.processor("def process(batch, ctx):\n    return []\n")
    assert rig.run()[:2] == (0, [])


# ---- replay ----------------------------------------------------------------------------------------------------------------
def test_a_retry_of_the_same_batch_replays_the_journal_without_calling_process(rig):
    rig.processor(COUNTER)
    first = rig.run(doc({"a": 1}))
    rig.processor("def process(batch, ctx):\n    raise AssertionError('must replay')\n")
    again = rig.run(doc({"a": 1}))
    assert first[0] == again[0] == 0 and first[1] == again[1]
    assert "replaying the journal" in again[2]
    assert json.loads((rig.state / "counter.json").read_text()) == {"n": 1}, "state advanced once"
    rig.processor(COUNTER)
    rc, emits, _ = rig.run(doc({"a": 1}), env={"HOOKSPOOL_BATCH_ID": "8", "HOOKSPOOL_DELIVERY_ID": "hs_demo_8"})
    assert rc == 0 and emits[0]["payload"]["n"] == 2 and emits[0]["dedup_id"] == "hs_demo_8:0"


def test_a_journal_written_but_not_applied_is_applied_on_the_retry(rig):
    rig.processor(COUNTER)
    rig.run()
    j = rig.journal()
    j["applied"] = False                                   # killed after the journal, before the state write
    (rig.state / "counter.json").unlink()                  # ... so the file is as the pass read it (absent)
    P._atomic_write(P._journal_path(str(rig.state), "7"), j)
    rc, emits, _ = rig.run()
    assert rc == 0 and json.loads((rig.state / "counter.json").read_text()) == {"n": 1}


def test_the_digest_guard_never_writes_an_old_journal_over_newer_state(rig):
    rig.processor(COUNTER)
    rig.run()
    j = rig.journal()
    j["applied"] = False
    P._atomic_write(P._journal_path(str(rig.state), "7"), j)
    (rig.state / "counter.json").write_text(json.dumps({"n": 99}))      # a later batch moved the state on
    rc, emits, err = rig.run()
    assert rc == 0 and len(emits) == 1
    assert json.loads((rig.state / "counter.json").read_text()) == {"n": 99}
    assert "not applying stale state to counter.json" in err


def test_a_failing_pass_leaves_no_journal_and_no_state(rig):
    rig.processor("""\
        def process(batch, ctx):
            ctx.save_state("counter", {"n": 5})
            raise RuntimeError("api down")
    """)
    rc, emits, err = rig.run()
    assert (rc, emits) == (75, [])
    assert not (rig.state / "counter.json").exists() and rig.journal() is None
    assert P.staged(str(rig.state / "counter.json")) is None, "staging is closed after a failure"


def test_a_corrupt_journal_is_ignored_and_process_runs(rig):
    rig.processor(COUNTER)
    (rig.state / "journal").mkdir(parents=True)
    (rig.state / "journal" / "7.json").write_text("{not json")
    rc, emits, _ = rig.run()
    assert rc == 0 and emits[0]["payload"]["n"] == 1


def test_old_journal_entries_are_pruned_after_a_run(rig):
    rig.processor("def process(batch, ctx):\n    return []\n")
    (rig.state / "journal").mkdir(parents=True)
    old = rig.state / "journal" / "1.json"
    old.write_text("{}")
    t = time.time()
    os.utime(old, (t - P.JOURNAL_KEEP_S - 5,) * 2)
    assert rig.run(clock=lambda: 0.0)[0] == 0, "pruning uses wall-clock time, not the processor's clock"
    assert not old.exists() and os.listdir(rig.state / "journal") == ["7.json"]


# ---- exit codes ------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("body,code", [
    ("ctx.fail_permanent('the mailbox is gone')", 65),
    ("raise Permanent('bad')", 65),
    ("raise Retryable('later')", 75),
    (f"raise ValueError({SENTINEL!r})", 75),
    ("raise SystemExit(0)", 75),
    ("return 'not a list'", 75),
    ("return [{'no': 'type'}]", 75),
    ("return [{'event_type': 'alert', 'bad': object()}]", 75),
])
def test_exit_codes(rig, body, code):
    rig.processor(f"""\
        from hookspool.processor import Permanent, Retryable

        def process(batch, ctx):
            {body}
    """)
    rc, emits, err = rig.run()
    assert (rc, emits) == (code, [])
    assert SENTINEL not in err, "an exception's text never reaches the log"
    assert rig.journal() is None
    if code == 65 and "fail_permanent" in body:
        assert "the mailbox is gone" in err


def test_fail_permanent_raises(rig):
    ctx = P.Context(instance="x")
    with pytest.raises(P.Permanent, match="why"):
        ctx.fail_permanent("why")


def test_environment_problems_are_retried(rig):
    rig.processor(COUNTER)
    for env in ({"HOOKSPOOL_SERVICE_DIR": str(rig.tmp / "missing")}, {"HOOKSPOOL_DELTA_ROUTES": "nope"},
                {"HOOKSPOOL_SETTINGS": "[1]"}, {"HOOKSPOOL_SECRET_REFS": "x"}):
        assert rig.run(env=env)[0] == 75, env
    out, err = io.StringIO(), io.StringIO()
    assert P.run("demo-a", env=rig.env, stdin=io.StringIO("not json"), out=out, err=err) == 75
    assert out.getvalue() == "" and "BatchError" in err.getvalue()


def test_a_package_without_process_is_an_error(rig):
    rig.processor("x = 1\n")
    assert rig.run()[0] == 75


# ---- unmapped event types ---------------------------------------------------------------------------------------------
def test_unmapped_event_types_are_dropped_with_one_log_line(rig):
    rig.processor("""\
        def process(batch, ctx):
            return [{"event_type": "alert"}, {"event_type": "spam"}, {"event_type": "spam"}, {"event_type": "noise"}]
    """)
    rc, emits, err = rig.run()
    assert rc == 0 and [e["event_type"] for e in emits] == ["alert"]
    lines = [ln for ln in err.splitlines() if "dropped" in ln]
    assert lines == ["hookspool.processor demo-a: batch 7: dropped 3 item(s) of unmapped event types: noise x1, spam x2"]


def test_with_no_routes_every_item_is_dropped(rig):
    rig.processor("def process(batch, ctx):\n    return [{'event_type': 'alert'}]\n")
    rc, emits, err = rig.run(env={"HOOKSPOOL_DELTA_ROUTES": ""})
    assert (rc, emits) == (0, []) and "dropped 1 item(s)" in err


# ---- stdout, as the real subprocess -------------------------------------------------------------------------------------
NOISY = """\
import os
import sys

print("chatter from import")


def process(batch, ctx):
    print("chatter", {SENTINEL!r})
    sys.stdout.write("more chatter\\n")
    os.write(1, b"fd one chatter\\n")
    ctx.log("seen " + str(len(batch.events)))
    return [{{"event_type": "alert", "n": 1}}]
""".format(SENTINEL=SENTINEL)


def test_nothing_but_emit_lines_reaches_stdout(rig):
    rig.processor(NOISY)
    r = rig.subprocess()
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert len(lines) == 1 and lines[0].startswith('{"enqueue":')
    assert parse_emits(r.stdout.encode())[0]["payload"] == {"event_type": "alert", "n": 1}
    assert "chatter from import" in r.stderr and "fd one chatter" in r.stderr
    assert "hookspool.processor demo-a: seen 1" in r.stderr


@pytest.mark.parametrize("body,code", [("ctx.fail_permanent('gone')", 65),
                                       ("from hookspool.processor import Permanent\n    raise Permanent('x')", 65),
                                       ("from hookspool.processor import Retryable\n    raise Retryable('x')", 75),
                                       ("raise KeyError('x')", 75)])
def test_the_entry_point_exit_codes(rig, body, code):
    rig.processor(f"import sys\n\n\ndef process(batch, ctx):\n    print('noise')\n    {body}\n")
    r = rig.subprocess()
    assert r.returncode == code, r.stderr
    assert r.stdout == ""


def test_the_entry_point_replays_and_checks_its_arguments(rig):
    rig.processor(COUNTER)
    a, b = rig.subprocess(), rig.subprocess()
    assert a.returncode == b.returncode == 0 and a.stdout == b.stdout and "replaying" in b.stderr
    e = {"PATH": os.environ["PATH"], "PYTHONPATH": ROOT, **rig.env}
    r = subprocess.run([sys.executable, "-m", "hookspool.processor"], input="{}", capture_output=True, text=True,
                       env=e, timeout=60)
    assert r.returncode == 75 and r.stdout == "" and "usage" in r.stderr
    r = subprocess.run([sys.executable, "-m", "hookspool.processor", "other"], input="{}", capture_output=True,
                       text=True, env=e, timeout=60)
    assert r.returncode == 75 and r.stdout == ""


# ---- settings ----------------------------------------------------------------------------------------------------------------
SHOW_SETTINGS = """\
def process(batch, ctx):
    return [{"event_type": "alert", "settings": dict(ctx.settings), "tz": ctx.settings.tz}]
"""


def test_settings_are_typed_from_the_environment_with_manifest_defaults(rig):
    rig.processor(SHOW_SETTINGS)
    rc, emits, _ = rig.run(env={"HOOKSPOOL_SETTINGS": json.dumps({"limit": 9, "who": ["a"]})})
    assert rc == 0
    assert emits[0]["payload"]["settings"] == {"tz": "UTC", "limit": 9, "loud": False, "who": ["a"]}
    assert emits[0]["payload"]["tz"] == "UTC"


@pytest.mark.parametrize("bad", [{"limit": "9"}, {"limit": True}, {"loud": 1}, {"who": "a"}, {"tz": 5}])
def test_a_setting_of_the_wrong_type_is_refused(rig, bad):
    rig.processor(SHOW_SETTINGS)
    rc, emits, err = rig.run(env={"HOOKSPOOL_SETTINGS": json.dumps(bad)})
    assert (rc, emits) == (75, []) and "BatchError" in err


def test_settings_are_read_only():
    s = P.Settings({"a": [1]})
    s["a"].append(2)
    assert s["a"] == [1] and s.a == [1] and len(s) == 1
    with pytest.raises(AttributeError):
        s.a = 2
    with pytest.raises(AttributeError):
        s.missing


def test_settings_without_a_manifest_are_passed_through(rig):
    (rig.pkg / "service.toml").unlink()
    rig.processor(SHOW_SETTINGS)
    rc, emits, _ = rig.run()
    assert rc == 0 and emits[0]["payload"]["settings"] == {"tz": "Europe/Paris"}


# ---- secrets, http, oauth ------------------------------------------------------------------------------------------------
def test_secrets_resolve_through_the_config_resolver_and_are_never_logged(rig):
    sec = rig.tmp / "token"
    sec.write_text(SENTINEL + "\n")
    rig.processor("""\
        def process(batch, ctx):
            v = ctx.secret("api_token")
            ctx.log("token length " + str(len(v)))
            try:
                ctx.secret("other")
            except Exception as e:
                ctx.log("other: " + e.__class__.__name__)
            return [{"event_type": "alert", "ok": v.endswith("7f3a")}]
    """)
    rc, emits, err = rig.run(env={"HOOKSPOOL_SECRET_REFS": json.dumps({"api_token": {"file": str(sec)}})})
    assert rc == 0 and emits[0]["payload"]["ok"] is True
    assert f"token length {len(SENTINEL)}" in err and "other: SecretError" in err and SENTINEL not in err


def test_http_and_oauth_come_from_the_extras_factory_lazily_and_runtime_state_persists(rig):
    calls = []

    class Http:
        def get_json(self, url):
            return {"url": url}

    def factory(state, resolver):
        calls.append(sorted(state))

        def oauth(name):
            state["tokens"] = {name: "access"}                 # what the network layer caches
            return "access-for-" + name
        return {"http": Http(), "oauth": oauth, "secret": resolver}
    rig.processor("""\
        def process(batch, ctx):
            return [{"event_type": "alert", "got": ctx.http.get_json("u"), "tok": ctx.oauth("cred")}]
    """)
    rc, emits, _ = rig.run(extras_factory=factory)
    assert rc == 0 and emits[0]["payload"]["got"] == {"url": "u"} and emits[0]["payload"]["tok"] == "access-for-cred"
    assert calls == [[]], "built once, on first use"
    assert json.loads((rig.state / ".runtime.json").read_text()) == {"tokens": {"cred": "access"}}
    rig.processor("""\
        def process(batch, ctx):
            ctx.oauth("cred2")
            raise RuntimeError("after the refresh")
    """)
    rc, _, _ = rig.run(env={"HOOKSPOOL_BATCH_ID": "8"}, extras_factory=factory)
    assert rc == 75 and calls[-1] == ["tokens"]
    assert json.loads((rig.state / ".runtime.json").read_text()) == {"tokens": {"cred2": "access"}}, \
        "a refreshed or rotated token is kept even when the batch fails"


def test_a_processor_that_never_touches_the_network_never_builds_it(rig):
    rig.processor("def process(batch, ctx):\n    return []\n")
    assert rig.run(extras_factory=lambda s, r: pytest.fail("built"))[0] == 0


def test_the_default_extras_come_from_the_network_module(monkeypatch):
    import types
    name = P.__name__.rsplit(".", 1)[0] + ".net"            # the network module of the package
    fake = types.ModuleType(name)
    fake.make_context_extras = lambda state, resolver: {"http": ("http", state), "oauth": lambda n: n}
    monkeypatch.setitem(sys.modules, name, fake)
    ctx = P.Context(instance="x", runtime_state={"a": 1})
    assert ctx.http == ("http", {"a": 1}) and ctx.oauth("c") == "c"


# ---- state, lock, hooks --------------------------------------------------------------------------------------------------
def test_the_state_lock_is_held_while_process_runs(rig):
    rig.processor("""\
        import fcntl
        import os

        def process(batch, ctx):
            fd = os.open(os.path.join(ctx.settings["dir"], ".lock"), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = False
            except BlockingIOError:
                held = True
            finally:
                os.close(fd)
            return [{"event_type": "alert", "held": held}]
    """)
    rc, emits, _ = rig.run(env={"HOOKSPOOL_SETTINGS": json.dumps({"dir": str(rig.state)})})
    assert rc == 0 and emits[0]["payload"]["held"] is True
    fd = os.open(str(rig.state / ".lock"), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)             # released afterwards
    finally:
        os.close(fd)


def test_state_names_are_checked_and_need_a_state_dir(rig):
    for name in ("../x", ".hidden", "", "a/b"):
        rig.processor(f"def process(batch, ctx):\n    ctx.save_state({name!r}, 1)\n    return []\n")
        assert rig.run()[0] == 75, name
    rig.processor("def process(batch, ctx):\n    ctx.save_state('s', 1)\n    return []\n")
    assert rig.run(env={"HOOKSPOOL_STATE_DIR": ""})[0] == 75


def test_a_stateless_processor_runs_without_a_state_dir(rig):
    rig.processor("def process(batch, ctx):\n    return [{'event_type': 'alert'}]\n")
    rc, emits, _ = rig.run(env={"HOOKSPOOL_STATE_DIR": ""})
    assert rc == 0 and len(emits) == 1 and not rig.state.exists()


def test_existing_state_files_are_read_by_name(rig):
    rig.state.mkdir()
    (rig.state / "gcal_push_state.json").write_text(json.dumps({"token": "t1"}))
    rig.processor("def process(batch, ctx):\n    return [{'event_type': 'alert', 's': ctx.load_state('gcal_push_state')}]\n")
    assert rig.run()[1][0]["payload"]["s"] == {"token": "t1"}


def test_a_hooks_module_is_loaded_by_path_and_must_be_safe(rig):
    hooks = rig.tmp / "hooks.py"
    hooks.write_text("def classify(m):\n    return {'tag': m}\n")
    os.chmod(hooks, 0o644)
    rig.processor("""\
        def process(batch, ctx):
            return [{"event_type": "alert", "h": ctx.hooks.classify("x") if ctx.hooks else None}]
    """)
    rc, emits, _ = rig.run(env={"HOOKSPOOL_SETTINGS": json.dumps({"hooks": str(hooks)})})
    assert rc == 0 and emits[0]["payload"]["h"] == {"tag": "x"}
    assert rig.run(env={"HOOKSPOOL_SETTINGS": "{}", "HOOKSPOOL_BATCH_ID": "8"})[1][0]["payload"]["h"] is None
    os.chmod(hooks, 0o666)
    assert rig.run(env={"HOOKSPOOL_SETTINGS": json.dumps({"hooks": str(hooks)}), "HOOKSPOOL_BATCH_ID": "9"})[0] == 75
    os.chmod(hooks, 0o644)
    assert rig.run(env={"HOOKSPOOL_SETTINGS": json.dumps({"hooks": "hooks.py"}), "HOOKSPOOL_BATCH_ID": "10"})[0] == 75


def test_the_manifest_names_the_processor_module(rig):
    (rig.pkg / "service.toml").write_text(MANIFEST.replace('module = "processor"', 'module = "work"'))
    (rig.pkg / "work.py").write_text("def process(batch, ctx):\n    return [{'event_type': 'alert', 'm': 'work'}]\n")
    assert rig.run()[1][0]["payload"]["m"] == "work"
    (rig.pkg / "service.toml").write_text(MANIFEST.replace('module = "processor"', 'module = "../evil"'))
    assert rig.run(env={"HOOKSPOOL_BATCH_ID": "8"})[0] == 75


# ---- the deploy/scripts re-export --------------------------------------------------------------------------------------
def test_hookspool_batch_is_a_re_export_of_the_runtime(monkeypatch):
    monkeypatch.syspath_prepend(SCRIPTS)
    sys.modules.pop("hookspool_batch", None)
    try:
        hb = importlib.import_module("hookspool_batch")
        assert hb is P
        for name in ("read_document", "make_emit", "format_emit", "save", "staged", "staging", "run_batch",
                     "load_journal", "prune_journal", "destinations", "BatchError", "JOURNAL_KEEP_S"):
            assert hasattr(hb, name), name
    finally:
        sys.modules.pop("hookspool_batch", None)
