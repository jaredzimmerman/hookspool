"""T9 hardening: the small fixes found by the adversarial pass, each pinned by a test.

- the owner's collection script is gone from the tree (``test_ci_tools`` scans the rest);
- headers that cannot go on the wire are left out instead of becoming a transport loop, and a
  source's ``event_header`` cannot name a header hookspool sets;
- ``dispatch.lease_grace_s`` below the 5 s deadline margin is refused;
- the script kill path is bounded even when the group cannot be signalled;
- the intake's 405 closes the connection (its body is never read);
- text logs cannot be split by a value that came from a request;
- a config reload that fails in an unexpected way keeps the old config and opens ``config:file``.
"""
import io
import logging
import os
import subprocess
import sys
import time

import pytest

from _httpstub import Stub, doc_for, encode, make_ctx
from conftest import SENTINEL
from hookspool import config as C
from hookspool import logs as hs_logs
from hookspool.destinations import build

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---- the public tree -----------------------------------------------------------------------------------
def test_the_owner_collection_script_is_not_in_the_repo():
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert "collect.sh" not in files


# ---- header values -------------------------------------------------------------------------------------
@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.close()


@pytest.mark.parametrize("value", ["café-ok", "事件", "a\x00b", "a\x85b"])
def test_http_static_header_that_cannot_be_sent_is_left_out(make_config, tmp_path, stub, value):
    secret = tmp_path / "s"
    secret.write_text("audit-secret-1\n")
    cfg = make_config(destinations={"audit": {"type": "http", "url": stub.url + "/h", "allow_insecure": True,
                                              "secret": {"file": str(secret)}, "headers": {"X-Env": value}}})
    d = build(cfg, "audit")
    doc = doc_for()
    out = d.deliver(doc, encode(doc), "hs_x", make_ctx(wall=True))
    assert out.kind == "ok", out                       # never a transport loop
    sent = stub.requests[0]["headers"].get("X-Env")
    if value == "café-ok":
        assert sent is not None and d.check() == []
    else:
        assert sent is None and any("cannot be sent" in p for p in d.check())


@pytest.mark.parametrize("name,ok", [("X-GitHub-Event", True), ("X-Event-Type", True), ("webhook-id", False),
                                     ("Content-Type", False), ("X-Hookspool-Batch", False), ("Host", False),
                                     ("bad name", False), ("", False), ("x" * 65, False)])
def test_check_refuses_an_event_header_hookspool_sets(name, ok):
    cfg = C.merge(C.DEFAULTS, {"sources": {"s": {"module": "x", "action": "hermes:r", "event_header": name}}})
    rep = C.check(cfg, load_plugin=None)
    assert any("event_header" in e for e in rep.errors) is (not ok), rep.errors


def test_the_github_bobbin_sets_its_event_header():
    import sourcekit
    assert sourcekit.bobbin_source_cfg("github")["event_header"] == "X-GitHub-Event"
    import tomllib
    from hookspool import bobbin_loader
    with open(os.path.join(ROOT, "deploy/config.example.toml"), "rb") as f:
        example = tomllib.load(f)
    assert "github" in example["bobbins"]                 # the example's GitHub is an instance of the bobbin
    assert bobbin_loader.expand(C.merge(C.DEFAULTS, example))["sources"]["github"]["event_header"] == "X-GitHub-Event"


# ---- lease grace ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("grace,ok", [(0, False), (4.9, False), (5, True), (60, True)])
def test_lease_grace_below_the_deadline_margin_is_refused(grace, ok):
    rep = C.check(C.merge(C.DEFAULTS, {"dispatch": {"lease_grace_s": grace}}), load_plugin=None)
    assert any("lease_grace_s" in e for e in rep.errors) is (not ok)


# ---- script kill path ------------------------------------------------------------------------------------
def test_script_timeout_is_bounded_when_the_group_cannot_be_signalled(make_config, tmp_path, monkeypatch):
    """T4 round 2 note 5: a PermissionError from killpg used to skip the kill, and the wait after
    it had no bound. Now the leader is killed directly and the wait is bounded."""
    from hookspool.destinations import script as sc
    p = tmp_path / "s.py"
    p.write_text("import sys, time\nsys.stdin.read()\ntime.sleep(60)\n")
    cfg = make_config(destinations={"script:t": {"argv": [sys.executable, str(p)], "timeout_s": 1}})
    d = build(cfg, "script:t")

    def denied(pgid, sig):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(sc.os, "killpg", denied)
    t0 = time.monotonic()
    doc = doc_for()
    out = d.deliver(doc, encode(doc), "hs_x", make_ctx(wall=True))
    assert (out.kind, out.klass) == ("retry", "transport") and time.monotonic() - t0 < 1 + sc.KILL_WAIT_S + 1


# ---- intake 405 -----------------------------------------------------------------------------------------
def test_405_on_health_closes_the_connection(make_config, make_store):
    from hookspool.intake import Intake
    cfg = make_config()
    ik = Intake(cfg, make_store(cfg))
    for path in ("/healthz", "/metrics"):
        r = ik.handle("POST", path, [("Content-Length", "3")], io.BytesIO(b"abc"))
        assert r.status == 405 and r.headers.get("Connection") == "close"


# ---- log lines ------------------------------------------------------------------------------------------
def test_text_log_lines_cannot_be_split_by_request_values():
    rec = logging.LogRecord("hookspool.intake", logging.WARNING, __file__, 1, "unverified\nrequest", None, None)
    rec.reason = "remote 192.0.2.4\n2026-10-23T00:00:00 error   intake: forged"
    rec.source = "s\r\nx"
    line = hs_logs.TextFormatter().format(rec)
    assert "\n" not in line and "\r" not in line and "forged" in line


# ---- config reload ---------------------------------------------------------------------------------------
def test_a_reload_that_fails_unexpectedly_keeps_the_old_config(rig, tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("[alerts]\ndry = true\n")
    r = rig(config_path=str(cfg_path))
    old = r.d.config
    os.utime(cfg_path, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))

    def boom(*a, **k):
        raise TypeError("checker bug")
    monkeypatch.setattr(C, "check", boom)
    r.d.tick()
    assert r.d.config is old
    iss = r.store.issue_get("config:file")
    assert iss is not None and iss["state"] == "open" and "TypeError" in iss["last_error"]
    assert SENTINEL not in (iss["last_error"] or "")
