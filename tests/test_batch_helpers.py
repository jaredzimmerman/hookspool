"""``deploy/scripts/hookspool_batch.py``: the re-export of ``hookspool.processor`` that stand-alone scripts import
(ARCHITECTURE 6.6, 14.3.4): the document, emits, staging and the journal that makes a retry replay instead of losing a
result. The Gmail and Calendar scripts that used these helpers are now bobbins (``tests/bobbins/``)."""
from __future__ import annotations

import importlib
import io
import json
import os
import sys
import time

import pytest

from conftest import SENTINEL

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "deploy", "scripts")




# ---- hookspool_batch ---------------------------------------------------------------------------------------
@pytest.fixture
def hb(monkeypatch):
    monkeypatch.syspath_prepend(SCRIPTS)
    sys.modules.pop("hookspool_batch", None)
    mod = importlib.import_module("hookspool_batch")
    yield mod
    sys.modules.pop("hookspool_batch", None)


def doc(*events, **kw):
    return {"source": "s", "batch_id": 7, "delivery_id": "hs_inst_7", "events": list(events), **kw}


def test_read_document_validates_the_shape(hb):
    assert hb.read_document(io.StringIO(json.dumps(doc({"a": 1}))))["batch_id"] == 7
    for bad in ("", "not json", "[]", '{"events": 5}', '{"count": 1}'):
        with pytest.raises(hb.BatchError):
            hb.read_document(io.StringIO(bad))
    assert hb.events(doc({"a": 1}, "x", None, {"b": 2})) == [{"a": 1}, {"b": 2}]


def test_ids_come_from_the_environment_then_the_document(hb, monkeypatch):
    monkeypatch.setenv("HOOKSPOOL_BATCH_ID", "42")
    monkeypatch.setenv("HOOKSPOOL_DELIVERY_ID", "hs_x_42")
    assert (hb.batch_id(doc()), hb.delivery_id(doc())) == ("42", "hs_x_42")
    monkeypatch.delenv("HOOKSPOOL_BATCH_ID")
    monkeypatch.delenv("HOOKSPOOL_DELIVERY_ID")
    assert (hb.batch_id(doc()), hb.delivery_id(doc())) == ("7", "hs_inst_7")
    with pytest.raises(hb.BatchError):
        hb.batch_id({})


def test_state_dir_comes_from_the_environment_or_the_default(hb, monkeypatch):
    monkeypatch.delenv("HOOKSPOOL_STATE_DIR", raising=False)
    with pytest.raises(hb.BatchError):
        hb.state_dir()
    assert hb.state_dir("/d") == "/d"
    monkeypatch.setenv("HOOKSPOOL_STATE_DIR", "/e")
    assert hb.state_dir("/d") == "/e"


def test_routes_map_event_types_to_destinations(hb, monkeypatch):
    assert hb.destinations() == {}
    monkeypatch.setenv("HOOKSPOOL_DELTA_ROUTES", json.dumps({"conflict": "hermes:calendar"}))
    assert hb.destinations() == {"conflict": "hermes:calendar"}
    for bad in ("nope", "[]", '{"a": 1}', '{"a": ""}'):
        monkeypatch.setenv("HOOKSPOOL_DELTA_ROUTES", bad)
        with pytest.raises(hb.BatchError):
            hb.destinations()


def test_make_emit_shape(hb):
    e = hb.make_emit("gcal", "k", {"event_type": "conflict", "n": 1}, 2, "hs_i_7", {"conflict": "hermes:calendar"})
    assert e == {"source": "gcal", "key": "k", "action": "hermes:calendar", "route": "calendar",
                 "payload": {"event_type": "conflict", "n": 1}, "event_type": "conflict", "dedup_id": "hs_i_7:2",
                 "quiet_s": 0, "max_s": 0}
    d = hb.make_emit("gcal", "k", {"event_type": "brief"}, 0, "d", {})
    assert d["action"] == "hermes:brief" and d["route"] == "brief"
    assert hb.make_emit("gcal", "", {"event_type": "x"}, 0, "d", {})["key"] == "x"
    with pytest.raises(hb.BatchError):
        hb.make_emit("gcal", "k", {"n": 1}, 0, "d", {})


def test_format_emit_is_one_line_the_dispatcher_parses(hb):
    from hookspool.destinations.script import parse_emits
    e = hb.make_emit("gmail", "a:b", {"event_type": "scheduling", "excerpt": "line1\nline2 é " + SENTINEL}, 0, "d", {})
    line = hb.format_emit(e)
    assert "\n" not in line and line.isascii()
    assert parse_emits((line + "\n").encode()) == [e]
    with pytest.raises(hb.BatchError):
        hb.format_emit(dict(e, surprise=1))


def test_staging_holds_writes_until_applied(hb):
    wrote = []
    hb.save("/p", {"a": 1}, lambda p, o: wrote.append((p, o)))
    assert wrote == [("/p", {"a": 1})]                    # no batch running: written through
    with hb.staging() as st:
        obj = {"a": [1]}
        hb.save("/q", obj, lambda p, o: wrote.append((p, o)))
        obj["a"].append(2)                                 # later mutation must not leak into the stage
        assert st == {"/q": {"a": [1]}} and hb.staged("/q") == {"a": [1]} and hb.staged("/none") is None
        assert wrote == [("/p", {"a": 1})]
    assert hb.staged("/q") is None


def run(hb, d, work, writer, tmp_path, env=None, **kw):
    out = io.StringIO()
    rc = hb.run_batch(d, work, writer, default_state_dir=str(tmp_path / "st"), out=out, **kw)
    return rc, [json.loads(line)["enqueue"] for line in out.getvalue().splitlines()]


def test_run_batch_stages_journals_applies_then_emits(hb, tmp_path, monkeypatch):
    monkeypatch.setenv("HOOKSPOOL_BATCH_ID", "7")
    order = []

    def writer(p, o):
        order.append(("write", os.path.basename(p), os.path.exists(tmp_path / "st" / "journal" / "7.json")))

    def work():
        hb.save(str(tmp_path / "state.json"), {"h": 1}, writer)
        order.append(("work",))
        return [hb.make_emit("s", "k", {"event_type": "x"}, 0, "d", {})]
    rc, emits = run(hb, doc(), work, writer, tmp_path)
    assert rc == 0 and [e["key"] for e in emits] == ["k"]
    assert order == [("work",), ("write", "state.json", True)], "state is written only after the journal exists"
    j = json.loads((tmp_path / "st" / "journal" / "7.json").read_text())
    assert j["applied"] is True and j["emits"][0]["key"] == "k" and j["state"] == {str(tmp_path / "state.json"): {"h": 1}}
    assert oct(os.stat(tmp_path / "st" / "journal" / "7.json").st_mode & 0o777) == "0o600"


def test_a_retry_replays_the_journal_without_running_the_work_again(hb, tmp_path, monkeypatch):
    monkeypatch.setenv("HOOKSPOOL_BATCH_ID", "7")
    calls, writes = [], []

    def work():
        calls.append(1)
        hb.save("/s", {"n": len(calls)}, lambda p, o: None)
        return [hb.make_emit("s", "k", {"event_type": "x"}, 0, "d", {})]
    w = lambda p, o: writes.append(o)      # noqa: E731
    first = run(hb, doc(), work, w, tmp_path)
    again = run(hb, doc(), work, w, tmp_path)
    assert calls == [1] and writes == [{"n": 1}], "work ran once and the state was applied once"
    assert first == again


def test_a_crash_after_the_journal_before_the_state_write_replays_both(hb, tmp_path, monkeypatch):
    monkeypatch.setenv("HOOKSPOOL_BATCH_ID", "7")
    calls, writes = [], []

    def work():
        calls.append(1)
        hb.save("/s", {"n": 1}, lambda p, o: None)
        return [hb.make_emit("s", "k", {"event_type": "x"}, 0, "d", {})]

    def dying(p, o):
        raise OSError("killed while applying the state")
    with pytest.raises(OSError):
        run(hb, doc(), work, dying, tmp_path)
    j = json.loads((tmp_path / "st" / "journal" / "7.json").read_text())
    assert j["applied"] is False and len(j["emits"]) == 1, "the result is durable before the state moves"
    rc, emits = run(hb, doc(), work, lambda p, o: writes.append(o), tmp_path)
    assert rc == 0 and calls == [1] and writes == [{"n": 1}] and len(emits) == 1


def test_a_failing_work_leaves_no_journal_and_no_state(hb, tmp_path, monkeypatch):
    monkeypatch.setenv("HOOKSPOOL_BATCH_ID", "7")
    writes = []

    def work():
        hb.save("/s", {"n": 1}, lambda p, o: writes.append(o))
        raise RuntimeError("api down")
    with pytest.raises(RuntimeError):
        run(hb, doc(), work, lambda p, o: writes.append(o), tmp_path)
    assert writes == [] and not (tmp_path / "st" / "journal").exists()
    assert hb.staged("/s") is None, "staging is closed after a failure"


def test_an_applied_journal_never_rewinds_state_on_a_later_replay(hb, tmp_path, monkeypatch):
    monkeypatch.setenv("HOOKSPOOL_BATCH_ID", "7")
    writes = []
    run(hb, doc(), lambda: (hb.save("/s", {"n": 1}, lambda p, o: None), [])[1], lambda p, o: writes.append(o), tmp_path)
    assert writes == [{"n": 1}]
    run(hb, doc(), lambda: pytest.fail("must replay"), lambda p, o: writes.append(("again", o)), tmp_path)
    assert writes == [{"n": 1}]


def test_journal_entries_are_pruned_by_age_and_names_are_sanitised(hb, tmp_path, monkeypatch):
    sd = str(tmp_path / "st")
    monkeypatch.setenv("HOOKSPOOL_BATCH_ID", "../evil/7")
    run(hb, doc(), lambda: [], lambda p, o: None, tmp_path)
    names = os.listdir(os.path.join(sd, "journal"))
    assert names == [".._evil_7.json"], names
    old = os.path.join(sd, "journal", "old.json")
    open(old, "w").write("{}")
    os.utime(old, (time.time() - hb.JOURNAL_KEEP_S - 5,) * 2)
    assert hb.prune_journal(sd) == 1 and not os.path.exists(old) and os.listdir(os.path.join(sd, "journal")) == names


def test_a_corrupt_journal_is_ignored_and_the_work_runs(hb, tmp_path, monkeypatch):
    monkeypatch.setenv("HOOKSPOOL_BATCH_ID", "7")
    p = tmp_path / "st" / "journal"
    p.mkdir(parents=True)
    (p / "7.json").write_text("{not json")
    calls = []
    run(hb, doc(), lambda: calls.append(1) or [], lambda a, b: None, tmp_path)
    assert calls == [1]
