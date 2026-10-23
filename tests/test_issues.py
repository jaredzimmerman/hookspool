"""Issues and alerts (C2, ARCHITECTURE §11.4): the lifecycle open -> acked -> resolved,
``ignored`` (never alerts or reopens), one alert per open issue sent from the alert thread
with ``alerted_at`` set only on exit 0, the persisted per-kind rate limit, and dry mode."""
import sys
import threading

import pytest

from _dispatchkit import DEST
from conftest import SENTINEL, T0
from hookspool import dispatch as dispatch_mod
from hookspool.destinations.base import Outcome

RATE = 1800


# ---- helpers -----------------------------------------------------------------------------------------
class AlertCmd:
    """A real alert command: appends its last argument to a log and exits with the code in
    a file. ``calls`` also records the thread each ``subprocess.run`` ran on."""

    def __init__(self, tmp_path, monkeypatch):
        self.log = tmp_path / "alerts.log"
        self.code = tmp_path / "alert.code"
        self.code.write_text("0")
        self.script = tmp_path / "alert.py"
        self.script.write_text(
            "import sys\n"
            f"open({str(self.log)!r}, 'a').write(sys.argv[-1].replace('\\n', ' ') + '\\n')\n"
            f"sys.exit(int(open({str(self.code)!r}).read()))\n")
        self.threads: list[str] = []
        real = dispatch_mod.subprocess.run

        def run(argv, *a, **kw):
            self.threads.append(threading.current_thread().name)
            return real(argv, *a, **kw)
        monkeypatch.setattr(dispatch_mod.subprocess, "run", run)
        monkeypatch.delenv("HOOKSPOOL_ALERT_DRY", raising=False)

    @property
    def argv(self) -> list[str]:
        return [sys.executable, "-I", str(self.script)]

    def exit_with(self, code: int) -> None:
        self.code.write_text(str(code))

    def lines(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []


def live(rig, cmd, **over):
    alerts = {"dry": False, "argv": cmd.argv, "rate_limit_s": RATE, "timeout_s": 20}
    alerts.update(over)
    return rig({"alerts": alerts})


def step(r) -> None:
    """One poll, then wait for the alert thread."""
    r.d.tick()
    r.d.flush_alerts()


def open_issue(r, fp=f"delivery:{DEST}:unreachable", kind="delivery", err="destination down"):
    return r.store.issue_open(fp, kind, destination=DEST, error=err, now=r.clock.t)


# ---- lifecycle -----------------------------------------------------------------------------------------
def test_lifecycle_open_acked_resolved_and_reopen(rig):
    r = rig(start=False)
    iid, opened = open_issue(r)
    assert opened and r.store.issue_get(iid)["state"] == "open"
    assert r.store.issue_ack(iid, now=T0 + 1)
    assert r.store.issue_get(iid)["state"] == "acked"
    assert not r.store.issue_ack(iid)                         # acked -> acked is not a transition
    _, opened = open_issue(r)                                 # recurrence while acked: bump, stay acked
    i = r.store.issue_get(iid)
    assert not opened and i["state"] == "acked" and i["count"] == 2
    assert r.store.issue_resolve(iid, now=T0 + 2)
    assert r.store.issue_get(iid)["state"] == "resolved"
    assert not r.store.issue_resolve(iid)
    r.store.issue_mark_alerted(iid, now=T0 + 3)
    iid2, opened = open_issue(r)                              # reopen: same row, alerted_at reset
    i = r.store.issue_get(iid)
    assert iid2 == iid and opened and i["state"] == "open" and i["alerted_at"] is None and i["count"] == 3


def test_delivery_issue_opens_on_failure_and_resolves_on_success(rig):
    r = rig({"dispatch": {"max_attempts": 10}})
    r.stub().script(*[Outcome.batch("exit 1", status=1)] * 3)
    a = r.enqueue()
    for _ in range(3):
        r.d.drain_once()
        r.clock.advance(400)
    fp = f"delivery:{DEST}:batch"
    assert r.store.issue_get(fp)["state"] == "open"            # a counted failure streak of 3
    r.store.issue_ack(fp)
    r.d.drain_once()                                          # the 4th attempt succeeds
    assert r.batch(a.batch_id)["state"] == "delivered"
    assert r.store.issue_get(fp)["state"] == "resolved"


def test_ignored_never_alerts_or_reopens(rig):
    r = rig()
    iid, _ = open_issue(r)
    assert r.store.issue_ignore(iid)
    step(r)
    assert r.d.alerts_sent == [] and r.store.issue_get(iid)["alerted_at"] is None
    for _ in range(3):                                         # it keeps happening: still ignored
        _, opened = open_issue(r)
        assert not opened
        r.clock.advance(RATE + 1)
        step(r)
    i = r.store.issue_get(iid)
    assert i["state"] == "ignored" and i["count"] == 4 and i["alerted_at"] is None
    assert r.d.alerts_sent == []
    assert not r.store.issue_resolve(iid)                      # auto-resolve does not touch it either
    assert not r.store.issue_ack(iid)


def test_ignore_after_resolve_stays_ignored_through_a_dispatcher_failure(rig):
    r = rig()
    r.stub().script(Outcome.permanent("rejected", status=422))
    r.enqueue()
    r.d.drain_once()
    fp = f"delivery:{DEST}:permanent"
    r.d.flush_alerts()
    r.store.issue_resolve(fp)
    r.store.issue_ignore(fp)
    sent = len(r.d.alerts_sent)
    r.stub().script(Outcome.permanent("rejected", status=422))
    r.enqueue("k2")
    r.clock.advance(RATE + 1)
    r.d.drain_once()
    step(r)
    assert r.store.issue_get(fp)["state"] == "ignored" and len(r.d.alerts_sent) == sent


# ---- alerts: dry mode ----------------------------------------------------------------------------------
def test_dry_mode_records_once_and_runs_nothing(rig, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("dry mode ran a command")
    monkeypatch.setattr(dispatch_mod.subprocess, "run", boom)
    r = rig({"alerts": {"dry": True, "argv": ["/nonexistent/alert"]}})
    r.stub().script(Outcome.permanent("rejected", status=422))
    a = r.enqueue()
    r.d.drain_once()
    r.d.flush_alerts()
    fp = f"delivery:{DEST}:permanent"
    assert [f for f, _ in r.d.alerts_sent] == [fp]
    text = r.d.alerts_sent[0][1]
    assert f"hookspool replay {a.batch_id}" in text and SENTINEL not in text
    assert r.store.issue_get(fp)["alerted_at"] == pytest.approx(T0)
    for _ in range(3):                                         # once per open issue
        r.clock.advance(RATE + 1)
        step(r)
    assert len(r.d.alerts_sent) == 1


def test_env_forces_dry_mode(rig, monkeypatch):
    monkeypatch.setattr(dispatch_mod.subprocess, "run", lambda *a, **k: pytest.fail("ran a command"))
    monkeypatch.setenv("HOOKSPOOL_ALERT_DRY", "1")
    r = rig({"alerts": {"dry": False, "argv": ["/nonexistent/alert"]}})
    open_issue(r)
    step(r)
    assert len(r.d.alerts_sent) == 1


# ---- alerts: a real command ----------------------------------------------------------------------------
def test_alert_runs_argv_from_the_alert_thread_once_on_exit_0(rig, tmp_path, monkeypatch):
    cmd = AlertCmd(tmp_path, monkeypatch)
    r = live(rig, cmd)
    iid, _ = open_issue(r, err="destination down")
    step(r)
    assert cmd.lines() == [f"hookspool delivery issue #{iid} delivery:{DEST}:unreachable: destination down"]
    assert cmd.threads and all(t.startswith("hookspool-alert") for t in cmd.threads)
    assert threading.main_thread().name not in cmd.threads
    assert r.store.issue_get(iid)["alerted_at"] == pytest.approx(T0)
    for _ in range(3):
        r.clock.advance(RATE + 1)
        step(r)
    assert len(cmd.lines()) == 1


def test_failing_alert_is_retried_and_alerted_at_waits_for_exit_0(rig, tmp_path, monkeypatch):
    cmd = AlertCmd(tmp_path, monkeypatch)
    cmd.exit_with(1)
    r = live(rig, cmd)
    iid, _ = open_issue(r)
    step(r)
    assert len(cmd.lines()) == 1 and r.store.issue_get(iid)["alerted_at"] is None
    r.clock.advance(30)                                         # retry waits ALERT_RETRY_S
    step(r)
    assert len(cmd.lines()) == 1
    r.clock.advance(dispatch_mod.ALERT_RETRY_S)
    step(r)                                                     # still failing: retried, not marked
    assert len(cmd.lines()) == 2 and r.store.issue_get(iid)["alerted_at"] is None
    cmd.exit_with(0)
    r.clock.advance(dispatch_mod.ALERT_RETRY_S + 1)             # well inside the rate window:
    step(r)                                                     # nothing went out, so it is not gated
    assert len(cmd.lines()) == 3
    assert r.store.issue_get(iid)["alerted_at"] == pytest.approx(r.clock.t)
    r.clock.advance(RATE + 1)
    step(r)
    assert len(cmd.lines()) == 3


def test_alert_command_timeout_is_a_failure(rig, tmp_path, monkeypatch):
    AlertCmd(tmp_path, monkeypatch)
    slow = tmp_path / "slow.py"
    slow.write_text("import time\ntime.sleep(10)\n")
    r = rig({"alerts": {"dry": False, "argv": [sys.executable, "-I", str(slow)], "rate_limit_s": RATE,
                        "timeout_s": 0.3}})
    iid, _ = open_issue(r)
    step(r)
    assert r.store.issue_get(iid)["alerted_at"] is None and r.d.alerts_sent == []


def test_empty_argv_sends_nothing(rig, tmp_path, monkeypatch):
    cmd = AlertCmd(tmp_path, monkeypatch)
    r = live(rig, cmd, argv=[])
    iid, _ = open_issue(r)
    step(r)
    assert cmd.threads == [] and r.store.issue_get(iid)["alerted_at"] is None


def test_reopen_after_resolve_alerts_again(rig, tmp_path, monkeypatch):
    cmd = AlertCmd(tmp_path, monkeypatch)
    r = live(rig, cmd)
    iid, _ = open_issue(r)
    step(r)
    r.store.issue_resolve(iid)
    r.clock.advance(10)
    open_issue(r)                                               # reopened inside the rate window
    assert r.store.issue_get(iid)["alerted_at"] is None
    step(r)
    assert len(cmd.lines()) == 1                                # gated by the per-kind rate limit
    r.clock.advance(RATE)
    step(r)
    assert len(cmd.lines()) == 2
    assert "(+1 suppressed since the last alert of this kind)" in cmd.lines()[1]
    assert r.store.issue_get(iid)["alerted_at"] == pytest.approx(r.clock.t)


# ---- the rate limit: per kind, persisted ---------------------------------------------------------------
def test_rate_limit_is_per_kind_and_survives_a_restart(rig, tmp_path, monkeypatch):
    cmd = AlertCmd(tmp_path, monkeypatch)
    r = live(rig, cmd)
    a, _ = open_issue(r, f"delivery:{DEST}:unreachable")
    step(r)
    r.clock.advance(10)
    b, _ = open_issue(r, "delivery:hermes:other:permanent")     # same kind: gated
    c, _ = open_issue(r, "config:file", kind="config")           # another kind: goes out
    step(r)
    assert [ln.split()[3] for ln in cmd.lines()] == [f"#{a}", f"#{c}"]
    assert r.store.issue_get(b)["alerted_at"] is None
    # restart: a new dispatcher on the same spool keeps the window
    r.d.close()
    r.clock.advance(100)
    r.d = r.dispatcher()
    r.d.start()
    step(r)
    assert len(cmd.lines()) == 2
    r.clock.advance(RATE - 110)
    step(r)
    assert len(cmd.lines()) == 3 and cmd.lines()[2].split()[3] == f"#{b}"
    assert r.store.issue_get(b)["alerted_at"] == pytest.approx(r.clock.t)


# ---- silent sources ------------------------------------------------------------------------------------
def test_silent_source_baseline_survives_restarts(rig):
    day = 86400
    r = rig({"sources": {"quiet": {"expect_every_s": day}}})
    step(r)
    assert r.store.issue_get("source_silent:quiet") is None
    for _ in range(3):                                          # a restart every 9 h does not mask it
        r.d.close()
        r.clock.advance(9 * 3600)
        r.d = r.dispatcher()
        r.d.start()
        step(r)
    i = r.store.issue_get("source_silent:quiet")
    assert i is not None and i["state"] == "open" and i["kind"] == "source_silent"
    r.store.enqueue("quiet", "k", DEST, {"n": 1, "note": SENTINEL}, quiet_s=0, max_s=0)   # the next event
    r.clock.advance(61)
    step(r)
    assert r.store.issue_get("source_silent:quiet")["state"] == "resolved"
