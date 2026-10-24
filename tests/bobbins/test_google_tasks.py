"""The ``google.tasks`` poll bobbin (ARCHITECTURE 14.3, 14.4): its poller through the processor runtime (journal, staged
state, emits) against a scripted Google (``tests/_googlestub.py``), and the bobbin as a unit.

* the first pass records where every list stands and reports nothing; later passes report what changed;
* ``updatedMin`` follows the newest ``updated`` seen, a task is never reported twice, a replayed tick reads nothing;
* paging, completed / hidden / deleted tasks, ``include_titles``, the ``tasklists`` filter, lists that appear or go;
* the OAuth refresh (a stale token is refreshed once and cached), ``Retryable`` (75) / ``Permanent`` (65);
* the 14.3.7 rules, the poll manifest and the documents of 14.3.8.
"""
from __future__ import annotations

import calendar
import json
import os
import re

import pytest

import _bobbinrig
import _bobbinrules as R
import _googlestub

from hookspool import bobbin_loader as PK
from hookspool import config as C
from hookspool import net
from hookspool.bobbins.google.tasks import poller as tp
from hookspool.providers import google as G

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DIR = os.path.join(ROOT, "hookspool", "bobbins", "google", "tasks")
OUTPUTS = tp.OUTPUTS
T1 = "2026-10-20T08:00:00.000Z"
T2 = "2026-10-21T08:00:00.000Z"
T3 = "2026-10-22T08:00:00.000Z"
T4 = "2026-10-23T08:00:00.000Z"
T5 = "2026-10-24T08:00:00.000Z"
def back(ts):
    """``ts`` minus the poller's margin, as the poller writes ``updatedMin``."""
    return tp.stamp(tp.moment(ts) - tp.MARGIN)


TICK = {"tick": 1_800_000_000, "instance": "tasks"}


@pytest.fixture
def google():
    g = _googlestub.GoogleStub()
    yield g
    g.close()


@pytest.fixture
def make(tmp_path, google):
    """``make(**settings)`` -> a Rig for the google.tasks bobbin."""
    def build_rig(**over):
        cred = _bobbinrig.write_cred(tmp_path / "creds" / "tasks.json", google.port)
        settings = {"tasklists": [], "include_titles": False, **google.tasks_settings(), **over}
        return _bobbinrig.Rig(tmp_path, "google.tasks", OUTPUTS, settings, {"cred": cred})
    return build_rig


def lst(lid, title="My tasks"):
    return {"id": lid, "title": title, "updated": T1}


def task(tid, updated=T2, status="needsAction", due=None, deleted=False, hidden=False, title=None):
    t = {"id": tid, "updated": updated, "status": status}
    if due:
        t["due"] = due
    if deleted:
        t["deleted"] = True
    if hidden:
        t["hidden"] = True
    t["title"] = title if title is not None else f"Title of {tid}"
    return t


def setup_stub(google, **lists):
    google.tasklists = [lst(i, title=i.title()) for i in lists]
    google.tasks = {i: list(rows) for i, rows in lists.items()}


def tick(rig, bid="1", **kw):
    return rig.items(TICK, bid=bid, **kw)


def seeded(make, google, **lists):
    """A rig whose first pass has already run over ``lists`` (so the next pass reports changes)."""
    setup_stub(google, **lists)
    rig = make()
    assert tick(rig, bid="0") == []
    return rig


def list_calls(google, lid):
    return [r for r in google.requests if r["path"] == f"/tasks/v1/lists/{lid}/tasks"]


def ids(items, kind="task-changed"):
    return [i["task_id"] for i in items if i["event_type"] == kind]


# ---- the first pass --------------------------------------------------------------------------------------
def test_the_first_pass_records_where_each_list_stands_and_reports_nothing(make, google):
    setup_stub(google, alpha=[task("a1", T2), task("a2", T3, status="completed"), task("a3", T4, deleted=True)],
               beta=[])
    rig = make()
    assert tick(rig) == []
    st = rig.get_state(tp.STATE)["lists"]
    assert st == {"alpha": {"updated": T4, "seen": {"a3": T4}}, "beta": {"updated": "", "seen": {}}}
    first = list_calls(google, "alpha")[0]["query"]
    assert "updatedMin" not in first, "the bootstrap reads the whole list"
    assert (first["showCompleted"], first["showHidden"], first["showDeleted"]) == ("true", "true", "true")


def test_an_account_with_no_lists_stores_an_empty_state(make, google):
    rig = make()
    assert tick(rig) == [] and rig.get_state(tp.STATE) == {"lists": {}}


# ---- later passes ----------------------------------------------------------------------------------------
def test_a_later_pass_reports_what_changed_oldest_first_then_one_summary(make, google):
    rig = seeded(make, google, alpha=[task("a1", T2)], beta=[task("b1", T2)])
    google.tasks["alpha"] += [task("a2", T4, due="2026-10-30T00:00:00.000Z"), task("a3", T3, status="completed")]
    google.tasks["beta"].append(task("b2", T5, deleted=True))
    out = tick(rig, bid="2")
    assert [(i["event_type"], i.get("task_id")) for i in out] == [
        ("task-changed", "a3"), ("task-changed", "a2"), ("task-changed", "b2"), ("tasks-synced", None)]
    a3, a2, b2, done = out
    assert a3 == {"event_type": "task-changed", "source": "google-tasks", "key": "task:a3", "tasklist_id": "alpha",
                  "task_id": "a3", "status": "completed", "updated": T3, "due": "", "deleted": False}
    assert (a2["status"], a2["due"], a2["deleted"]) == ("needsAction", "2026-10-30T00:00:00.000Z", False)
    assert (b2["tasklist_id"], b2["deleted"]) == ("beta", True)
    assert done == {"event_type": "tasks-synced", "source": "google-tasks", "key": "tasks",
                    "tasklists": ["alpha", "beta"], "changed_count": 3}


def test_a_pass_with_no_changes_says_nothing_and_asks_from_the_stored_time(make, google):
    rig = seeded(make, google, alpha=[task("a1", T3)])
    assert tick(rig, bid="2") == []
    assert list_calls(google, "alpha")[-1]["query"]["updatedMin"] == back(T3)
    assert rig.get_state(tp.STATE)["lists"]["alpha"] == {"updated": T3, "seen": {"a1": T3}}


def test_updated_min_follows_the_newest_update_and_a_task_is_never_reported_twice(make, google):
    rig = seeded(make, google, alpha=[task("a1", T2)])
    google.tasks["alpha"].append(task("a2", T3))
    assert ids(tick(rig, bid="2")) == ["a2"]
    assert rig.get_state(tp.STATE)["lists"]["alpha"] == {"updated": T3, "seen": {"a2": T3}}
    # the filter is inclusive: a2 comes back at the stored instant and is skipped; a task that shares the instant is new
    google.tasks["alpha"].append(task("a3", T3))
    assert ids(tick(rig, bid="3")) == ["a3"]
    assert list_calls(google, "alpha")[-1]["query"]["updatedMin"] == back(T3)
    assert rig.get_state(tp.STATE)["lists"]["alpha"] == {"updated": T3, "seen": {"a2": T3, "a3": T3}}
    assert tick(rig, bid="4") == [], "both are remembered at that instant"
    google.tasks["alpha"][0] = task("a1", T4, status="completed")             # an old task edited again
    out = tick(rig, bid="5")
    assert ids(out) == ["a1"] and out[0]["status"] == "completed"


def test_a_task_edited_twice_between_passes_is_one_item_with_its_last_state(make, google):
    rig = seeded(make, google, alpha=[task("a1", T2)])
    google.tasks["alpha"][0] = task("a1", T4, status="completed")
    out = tick(rig, bid="2")
    assert ids(out) == ["a1"] and out[0]["status"] == "completed" and out[-1]["changed_count"] == 1


def test_a_task_reopened_and_a_task_deleted_are_reported_as_such(make, google):
    rig = seeded(make, google, alpha=[task("a1", T2, status="completed"), task("a2", T2)])
    google.tasks["alpha"][0] = task("a1", T3, status="needsAction")
    google.tasks["alpha"][1] = task("a2", T4, deleted=True)
    out = tick(rig, bid="2")
    assert [(i["task_id"], i["status"], i["deleted"]) for i in out[:2]] == [
        ("a1", "needsAction", False), ("a2", "needsAction", True)]


def test_completed_and_hidden_and_deleted_tasks_are_asked_for(make, google):
    rig = seeded(make, google, alpha=[task("a1", T2)])
    google.tasks["alpha"] += [task("h", T3, status="completed", hidden=True), task("d", T4, deleted=True)]
    assert ids(tick(rig, bid="2")) == ["h", "d"], "a task finished in Google's own apps is hidden"


# ---- retry and replay ------------------------------------------------------------------------------------
def test_a_replayed_tick_reports_the_same_items_without_reading_again(make, google):
    rig = seeded(make, google, alpha=[task("a1", T2)])
    google.tasks["alpha"].append(task("a2", T3))
    first = tick(rig, bid="2")
    calls = len(google.requests)
    again = tick(rig, bid="2")
    assert again == first and len(google.requests) == calls, "the journal answered"
    assert rig.get_state(tp.STATE)["lists"]["alpha"]["updated"] == T3


def test_a_retry_after_a_crash_between_journal_and_state_loses_nothing(make, google, monkeypatch):
    rig = seeded(make, google, alpha=[task("a1", T2)])
    google.tasks["alpha"].append(task("a2", T3))
    before = rig.get_state(tp.STATE)
    real = _bobbinrig.P._atomic_write
    state_path = rig.path(tp.STATE)

    def dying(path, obj):
        if path == state_path:
            raise OSError("killed while writing state")
        return real(path, obj)
    monkeypatch.setattr(_bobbinrig.P, "_atomic_write", dying)
    rc, emits, err = rig.run(TICK, bid="2")
    assert rc == 75 and emits == [] and rig.get_state(tp.STATE) == before, "the cursor did not move"
    monkeypatch.setattr(_bobbinrig.P, "_atomic_write", real)
    calls = len(google.requests)
    rc, emits, err = rig.run(TICK, bid="2")
    assert rc == 0 and ids([e["payload"] for e in emits]) == ["a2"] and len(google.requests) == calls
    assert rig.get_state(tp.STATE)["lists"]["alpha"]["updated"] == T3


# ---- paging ----------------------------------------------------------------------------------------------
def test_a_list_is_read_across_pages_and_the_cursor_is_the_newest_of_all_of_them(make, google):
    google.tasks_page_size = 2
    rig = seeded(make, google, alpha=[task("a0", T1)])
    google.tasks["alpha"] += [task(f"n{i}", t) for i, t in enumerate([T3, T2, T5, T4, T3])]
    out = tick(rig, bid="2")
    assert ids(out) == ["n1", "n0", "n4", "n3", "n2"] and out[-1]["changed_count"] == 5
    pages = [r["query"].get("pageToken") for r in list_calls(google, "alpha")[-3:]]
    assert pages == [None, "p2", "p4"]
    assert rig.get_state(tp.STATE)["lists"]["alpha"] == {"updated": T5, "seen": {"n2": T5}}


# ---- the overlap -----------------------------------------------------------------------------------------
def test_a_write_that_becomes_visible_late_inside_the_margin_is_still_reported_once(make, google):
    rig = seeded(make, google, alpha=[task("a1", T2)])
    google.tasks["alpha"].append(task("a2", T3))
    assert ids(tick(rig, bid="2")) == ["a2"]
    late = "2026-10-22T07:59:30.000Z"                           # stamped 30 s before a2, visible only after it
    google.tasks["alpha"].append(task("a3", late))
    out = tick(rig, bid="3")
    assert ids(out) == ["a3"] and out[-1]["changed_count"] == 1
    assert list_calls(google, "alpha")[-1]["query"]["updatedMin"] == back(T3)
    assert rig.get_state(tp.STATE)["lists"]["alpha"] == {"updated": T3, "seen": {"a2": T3, "a3": late}}
    assert tick(rig, bid="4") == [], "reported once"
    google.tasks["alpha"].append(task("a4", "2026-10-22T07:50:00.000Z"))   # later than the margin: out of reach
    assert tick(rig, bid="5") == []


def test_a_task_edited_again_inside_the_margin_is_reported_again(make, google):
    rig = seeded(make, google, alpha=[task("a1", T3)])
    google.tasks["alpha"][0] = task("a1", "2026-10-22T08:00:20.000Z", status="completed")
    out = tick(rig, bid="2")
    assert ids(out) == ["a1"] and out[0]["status"] == "completed"
    assert tick(rig, bid="3") == []


def test_tasks_older_than_the_margin_leave_the_kept_map(make, google):
    rig = seeded(make, google, alpha=[task("a1", T3)])
    google.tasks["alpha"].append(task("a2", "2026-10-22T08:00:30.000Z"))
    google.tasks["alpha"].append(task("a3", "2026-10-22T08:02:00.000Z"))
    assert ids(tick(rig, bid="2")) == ["a2", "a3"]
    assert rig.get_state(tp.STATE)["lists"]["alpha"] == {"updated": "2026-10-22T08:02:00.000Z",
                                                          "seen": {"a3": "2026-10-22T08:02:00.000Z"}}


def test_a_state_written_by_the_first_draft_with_ids_is_still_read(make, google):
    setup_stub(google, alpha=[task("a1", T2)])
    rig = make()
    rig.seed_state(tp.STATE, {"lists": {"alpha": {"updated": T2, "ids": ["a1"]}}})
    assert tick(rig) == []
    google.tasks["alpha"].append(task("a2", T3))
    assert ids(tick(rig, bid="2")) == ["a2"]


# ---- the page cap ----------------------------------------------------------------------------------------
def cap_pages(monkeypatch, pages):
    real = G.tasks_changes
    monkeypatch.setattr(G, "tasks_changes", lambda *a, **k: real(*a, **dict(k, max_pages=pages)))
    return real


def test_a_read_cut_short_reports_what_it_read_and_moves_the_cursor_to_the_newest_it_read(make, google, monkeypatch):
    google.tasks_page_size = 2
    rig = seeded(make, google, alpha=[task("a0", T1)])
    google.tasks["alpha"] += [task("x1", T3), task("x2", T4), task("x3", T5)]
    real = cap_pages(monkeypatch, 1)                           # the pass sees a0 and x1 only
    rc, emits, err = rig.run(TICK, bid="2")
    assert rc == 0 and "capped=1" in err
    assert ids([e["payload"] for e in emits]) == ["x1"]
    assert rig.get_state(tp.STATE)["lists"]["alpha"] == {"updated": T3, "seen": {"x1": T3}}
    monkeypatch.setattr(G, "tasks_changes", real)              # the cap is gone: the rest follows, x1 not again
    out = tick(rig, bid="3")
    assert ids(out) == ["x2", "x3"]
    assert list_calls(google, "alpha")[-1]["query"]["updatedMin"] == back(T3)
    assert rig.get_state(tp.STATE)["lists"]["alpha"]["updated"] == T5


def test_a_cap_that_stays_in_place_moves_the_list_on_and_reports_nothing_twice(make, google, monkeypatch):
    google.tasks_page_size = 2
    rig = seeded(make, google, alpha=[task("a0", T1)])
    t6 = "2026-10-25T08:00:00.000Z"
    google.tasks["alpha"] += [task("x2", T4), task("x1", T3), task("x3", T5), task("x4", t6)]   # in no time order
    cap_pages(monkeypatch, 1)                                  # every read stops after one page of two
    rc, emits, err = rig.run(TICK, bid="2")
    assert rc == 0 and "capped=1" in err
    assert ids([e["payload"] for e in emits]) == ["x2"]
    assert rig.get_state(tp.STATE)["lists"]["alpha"]["updated"] == T4
    rc, emits, err = rig.run(TICK, bid="3")                    # capped again: x2 not again, the cursor moves on
    assert rc == 0 and "capped=1" in err
    assert ids([e["payload"] for e in emits]) == ["x3"]
    assert list_calls(google, "alpha")[-1]["query"]["updatedMin"] == back(T4)
    assert rig.get_state(tp.STATE)["lists"]["alpha"]["updated"] == T5
    assert ids(tick(rig, bid="4")) == ["x4"]                   # the read fits again
    assert tick(rig, bid="5") == []
    # x1 (stamped before x2 and not in the first capped read) fell behind the window: the loss README Limits names


def test_a_cap_filled_inside_the_margin_keeps_every_task_read_and_steps_one_margin_on(make, google, monkeypatch):
    """More tasks inside one minute than a read returns, and fewer kept ids than a read: still nothing twice and
    nothing stuck."""
    monkeypatch.setattr(tp, "IDS_KEPT", 2)
    google.tasks_page_size = 3
    rig = seeded(make, google, alpha=[task("a0", T1)])
    at = [f"2026-10-22T08:00:0{n}.000Z" for n in range(5)]
    google.tasks["alpha"] += [task(f"x{n}", at[n]) for n in range(5)]
    cap_pages(monkeypatch, 1)                                  # every read stops after three tasks
    seen = []
    for n in range(2, 6):
        seen += ids(tick(rig, bid=str(n)))
    assert seen == ["x0", "x1", "x2", "x3", "x4"], "each one once, none stuck"
    calls = [c["query"].get("updatedMin") for c in list_calls(google, "alpha")]
    assert calls[-2:] == [back(at[2]), at[2]], "a read filled inside the margin steps one margin on"
    assert tick(rig, bid="6") == []


def test_a_list_too_big_to_read_whole_starts_from_the_tick_time_and_reports_no_history(make, google, monkeypatch):
    google.tasks_page_size = 2
    setup_stub(google, alpha=[task("a1", T2), task("a2", T3), task("a3", T4)])
    now = calendar.timegm((2026, 10, 25, 0, 0, 0))
    rig = make()
    real = cap_pages(monkeypatch, 1)
    assert tick(rig, clock=lambda: now) == []
    assert rig.get_state(tp.STATE)["lists"]["alpha"] == {"updated": "2026-10-25T00:00:00.000Z", "seen": {}}
    monkeypatch.setattr(G, "tasks_changes", real)
    google.tasks["alpha"].append(task("a4", "2026-10-25T00:00:30.000Z"))
    out = tick(rig, bid="2", clock=lambda: now + 300)
    assert ids(out) == ["a4"], "only what changed after the first pass"
    assert list_calls(google, "alpha")[-1]["query"]["updatedMin"] == "2026-10-24T23:59:00.000Z"


# ---- which lists ------------------------------------------------------------------------------------------
def test_a_tasklists_name_that_matches_no_list_is_counted_in_the_log(make, google):
    setup_stub(google, alpha=[task("a1", T2)])
    rig = make(tasklists=["alpha", "Typo", "other"])
    rc, emits, err = rig.run(TICK)
    assert rc == 0 and "unmatched=2" in err and "lists=1" in err
    rc, emits, err = make(tasklists=["alpha"]).run(TICK, bid="2")
    assert "unmatched" not in err


class ScriptedClient:
    """Answers ``client.get`` from a list, standing in for ``ctx.oauth(...)``."""

    def __init__(self, *pages):
        self.pages, self.calls = list(pages), []

    def get(self, url, params=None, label=""):
        self.calls.append((url, dict(params or {})))
        page = self.pages.pop(0)
        if isinstance(page, int):
            raise net.Permanent(f"HTTP {page}", status=page)
        return page


def test_a_read_cut_short_by_the_page_cap_says_so():
    client = ScriptedClient({"items": [{"id": "a"}], "nextPageToken": "x"}, {"items": [{"id": "b"}], "nextPageToken": "y"})
    tasks, finished = G.tasks_changes(client, "L", "", max_pages=2)
    assert [t["id"] for t in tasks] == ["a", "b"] and finished is False
    assert [c[1].get("pageToken") for c in client.calls] == [None, "x"]


def test_the_list_id_is_quoted_into_the_url_and_a_missing_list_is_tasklist_gone():
    client = ScriptedClient(404)
    with pytest.raises(G.TasklistGone):
        G.tasks_changes(client, "a/b c", "")
    assert client.calls[0][0].endswith("/lists/a%2Fb%20c/tasks")


def test_other_errors_from_the_helpers_are_raised_as_they_are():
    with pytest.raises(net.Permanent):
        G.tasks_changes(ScriptedClient(403), "L")


# ---- titles ----------------------------------------------------------------------------------------------
def test_titles_stay_with_google_unless_asked_for(make, google):
    rig = seeded(make, google, alpha=[task("a1", T2)])
    google.tasks["alpha"].append(task("a2", T3, title="Call the plumber"))
    out = tick(rig, bid="2")
    assert "title" not in out[0] and "plumber" not in json.dumps(out)
    q = list_calls(google, "alpha")[-1]["query"]
    assert "title" not in q["fields"]


def test_include_titles_adds_the_title_and_asks_for_it(make, google):
    rig = seeded(make, google, alpha=[task("a1", T2)])
    google.tasks["alpha"].append(task("a2", T3, title="Call the plumber"))
    rig = make(include_titles=True)
    out = tick(rig, bid="2")
    assert out[0]["title"] == "Call the plumber"
    assert "title" in list_calls(google, "alpha")[-1]["query"]["fields"]


def test_logs_carry_counts_never_titles(make, google):
    rig = seeded(make, google, alpha=[task("a1", T2)])
    google.tasks["alpha"].append(task("a2", T3, title="Secret Merger Plan"))
    rc, emits, err = rig.run(TICK, bid="2")
    assert rc == 0 and "Secret Merger Plan" not in err and "changed=1" in err


# ---- which lists -----------------------------------------------------------------------------------------
def test_tasklists_names_lists_by_id_or_by_title_ignoring_case(make, google):
    setup_stub(google, alpha=[task("a1", T2)], beta=[task("b1", T2)], gamma=[task("g1", T2)])
    rig = make(tasklists=["alpha", "BETA"])
    assert tick(rig) == []
    assert set(rig.get_state(tp.STATE)["lists"]) == {"alpha", "beta"}
    assert not list_calls(google, "gamma")
    for k in ("alpha", "beta", "gamma"):
        google.tasks[k].append(task(k + "2", T3))
    assert ids(tick(rig, bid="2")) == ["alpha2", "beta2"]


def test_a_list_added_later_is_recorded_not_reported_and_then_followed(make, google):
    rig = seeded(make, google, alpha=[task("a1", T2)])
    google.tasklists.append(lst("beta", "Beta"))
    google.tasks["beta"] = [task("b1", T3)]
    assert tick(rig, bid="2") == []
    assert rig.get_state(tp.STATE)["lists"]["beta"] == {"updated": T3, "seen": {"b1": T3}}
    google.tasks["beta"].append(task("b2", T4))
    assert ids(tick(rig, bid="3")) == ["b2"]


def test_a_list_that_is_gone_or_no_longer_named_is_dropped_from_the_state(make, google):
    rig = seeded(make, google, alpha=[task("a1", T2)], beta=[task("b1", T2)])
    google.tasklists = [lst("alpha")]
    assert tick(rig, bid="2") == []
    assert set(rig.get_state(tp.STATE)["lists"]) == {"alpha"}


def test_a_list_deleted_between_listing_and_reading_is_skipped(make, google):
    rig = seeded(make, google, alpha=[task("a1", T2)], beta=[task("b1", T2)])
    google.tasks["alpha"].append(task("a2", T3))
    google.tasks["beta"] = 404
    out = tick(rig, bid="2")
    assert ids(out) == ["a2"] and set(rig.get_state(tp.STATE)["lists"]) == {"alpha"}


# ---- OAuth and failures ----------------------------------------------------------------------------------
def test_the_credential_is_never_rewritten_and_the_access_token_is_cached(make, google, tmp_path):
    rig = seeded(make, google, alpha=[task("a1", T2)])
    cred = tmp_path / "creds" / "tasks.json"
    before = cred.read_text()
    tick(rig, bid="2")
    tick(rig, bid="3")
    assert cred.read_text() == before and google.token_calls == 1
    assert all(r["auth"] == "Bearer tok-refreshed" for r in google.requests if r["path"].startswith("/tasks"))


def test_a_stale_access_token_is_refreshed_once_and_the_pass_goes_on(make, google):
    setup_stub(google, alpha=[task("a1", T2)])
    rig = make()
    fp = net._fingerprint("test-client", "test-refresh")
    rig.seed_state(".runtime", {"oauth": {"cred": {"base": fp, "access_token": "tok-stale",
                                                   "expires_at": 4_000_000_000}}})
    google.reject_bearers = {"tok-stale"}
    assert tick(rig) == []
    assert google.token_calls == 1
    auths = [r["auth"] for r in google.requests if r["path"].startswith("/tasks")]
    assert auths[0] == "Bearer tok-stale" and set(auths[1:]) == {"Bearer tok-refreshed"}
    assert rig.get_state(tp.STATE)["lists"]["alpha"]["updated"] == T2
    tick(rig, bid="2")
    assert google.token_calls == 1, "the refreshed token is kept"


def test_a_server_error_is_a_retry_and_leaves_the_state(make, google):
    rig = seeded(make, google, alpha=[task("a1", T2)])
    before = rig.get_state(tp.STATE)
    google.tasks["alpha"] = 500
    rc, emits, err = rig.run(TICK, bid="2")
    assert rc == 75 and emits == [] and rig.get_state(tp.STATE) == before and "will be retried" in err
    assert not os.path.exists(rig.state / "journal" / "2.json")
    google.tasks["alpha"] = [task("a1", T2), task("a2", T3)]
    assert ids(tick(rig, bid="2")) == ["a2"], "the next attempt loses nothing"


def test_a_failing_list_call_is_a_retry_and_a_refusal_is_permanent(make, google):
    rig = seeded(make, google, alpha=[task("a1", T2)])
    google.tasklists = 503
    assert rig.run(TICK, bid="2")[0] == 75
    google.tasklists = 403
    rc, emits, err = rig.run(TICK, bid="3")
    assert rc == 65 and emits == [] and "permanent failure" in err


def test_a_missing_credential_is_permanent(make, google):
    rig = make()
    rig.env["HOOKSPOOL_SECRET_REFS"] = "{}"
    rc, emits, err = rig.run(TICK)
    assert rc == 65 and emits == []


def test_emits_follow_the_routes_and_an_unmapped_output_is_dropped(make, google):
    rig = seeded(make, google, alpha=[task("a1", T2)])
    google.tasks["alpha"].append(task("a2", T3))
    rc, emits, err = rig.run(TICK, bid="2", routes={"task-changed": "hermes:todo"})
    assert rc == 0 and [(e["event_type"], e["action"], e["key"]) for e in emits] == [
        ("task-changed", "hermes:todo", "task:a2")]
    assert "dropped 1 item(s) of unmapped event types: tasks-synced x1" in err


# ---- the bobbin as a unit (14.3.1, 14.3.7, 14.3.8, 14.4) --------------------------------------------------
def package():
    pkgs, errs = PK.discover({})
    assert errs == [], errs
    return pkgs["google.tasks"]


def test_the_manifest_is_a_valid_poll_manifest_and_the_outputs_are_the_ones_the_code_produces():
    pkg = package()
    assert pkg.errors == [] and pkg.builtin and not pkg.source and pkg.processor is None
    assert pkg.poll["every_s"] == 300 and pkg.poll.get("state", True) is True
    assert set(tp.OUTPUTS) == set(pkg.outputs)
    assert pkg.secrets["cred"]["processor"] is True
    prov = pkg.manifest["provider"]
    assert prov["no_push_note"] == "The Tasks API has no watch or push method."
    assert prov["no_push_docs"] == prov["api_docs"] == "https://developers.google.com/workspace/tasks/reference/rest"
    assert "webhook_docs" not in prov and "signature_docs" not in prov
    for f in ("README.md", "SETUP.md", "AGENTS.md", "bobbin.toml", "poller.py"):
        assert os.path.isfile(os.path.join(DIR, f)), f
    assert not os.path.exists(os.path.join(DIR, "source.py")) and not os.path.exists(os.path.join(DIR, "processor.py"))


def test_the_bobbin_calls_core_and_never_re_implements_it():
    assert R.violations(DIR) == []


def secret_files(tmp_path, *names):
    out = {}
    for n in names:
        p = tmp_path / f"{n}.secret"
        p.write_text("test-" + n + "\n")
        os.chmod(p, 0o600)
        out[n] = str(p)
    return out


def test_an_instance_expands_to_a_poll_source_and_a_poller_destination(tmp_path):
    s = secret_files(tmp_path, "cred")
    p = tmp_path / "config.toml"
    p.write_text(f'''[spool]
home = "{tmp_path / "home"}"
[alerts]
dry = true
[bobbins.tasks]
bobbin = "google.tasks"
every_s = 600
secrets.cred = {{ file = "{s['cred']}" }}
settings = {{ tasklists = ["Errands"], include_titles = true }}
outputs = {{ task-changed = "hermes:todo", tasks-synced = "script:refresh" }}
[destinations."script:refresh"]
argv = ["/bin/true"]
''')
    cfg = C.load_config(str(p))
    src, dst = cfg["sources"]["tasks"], cfg["destinations"]["script:tasks"]
    assert src["kind"] == "poll" and src["every_s"] == 600 and src["span"] == "immediate"
    assert src["action"] == "script:tasks" and "module" not in src
    assert dst["processor"] == "tasks" and dst["max_inflight"] == 1
    env = dst["env"]
    assert json.loads(env["HOOKSPOOL_SECRET_REFS"]) == {"cred": {"file": s["cred"]}}
    settings = json.loads(env["HOOKSPOOL_SETTINGS"])
    assert settings["tasklists"] == ["Errands"] and settings["include_titles"] is True
    assert settings["api_url"] == "https://tasks.googleapis.com/tasks/v1/"
    assert [e for e in C.check(cfg, check_secrets=True).errors if "tasks" in e] == []


def test_every_instance_default_is_five_minutes(tmp_path):
    s = secret_files(tmp_path, "cred")
    p = tmp_path / "config.toml"
    p.write_text(f'''[spool]
home = "{tmp_path / "home"}"
[alerts]
dry = true
[bobbins.tasks]
bobbin = "google.tasks"
secrets.cred = {{ file = "{s['cred']}" }}
outputs = {{ task-changed = "hermes:todo" }}
''')
    assert C.load_config(str(p))["sources"]["tasks"]["every_s"] == 300


def read(doc):
    with open(os.path.join(DIR, doc), encoding="utf-8") as f:
        return f.read()


def test_every_secret_and_required_setting_appears_in_the_setup_documents():
    pkg = package()
    needed = list(pkg.secrets) + [k for k, v in pkg.settings.items() if v.get("required") is True]
    for doc in ("SETUP.md", "AGENTS.md"):
        for n in needed:
            assert n in read(doc), f"{doc} does not mention {n}"


def test_the_documents_open_with_the_poll_frontmatter():
    want = "---\nbobbin: google.tasks\nprovider: Google Tasks\napi_docs: https://developers.google.com/workspace/tasks/reference/rest\n---\n"
    for doc in ("README.md", "SETUP.md", "AGENTS.md"):
        assert read(doc).startswith(want), doc


def test_the_agent_document_has_the_needs_list_and_numbered_steps():
    text = read("AGENTS.md")
    assert "## Needs from the human" in text
    steps = re.findall(r"^(\d+)\. \*\*", text, re.M)
    assert steps == [str(i) for i in range(1, len(steps) + 1)] and len(steps) >= 5
    assert "`outward`" not in text, "nothing is registered at Google"
    for placeholder in re.findall(r"<([^>\s]+)>", text):
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", placeholder), placeholder


@pytest.mark.parametrize("doc", ["SETUP.md", "AGENTS.md"])
def test_the_configuration_shown_in_the_document_expands(doc, tmp_path):
    blocks = [b for b in re.findall(r"```toml\n(.*?)```", read(doc), re.S) if "[bobbins.tasks]" in b]
    assert len(blocks) == 1
    sd = tmp_path / "secrets"
    sd.mkdir()
    (sd / "owner.json").write_text("x")
    os.chmod(sd / "owner.json", 0o600)
    block = (blocks[0].replace("<CRED_FILE>", str(sd / "owner.json")).replace("/etc/hookspool/google", str(sd))
             .replace("<DESTINATION>", "hermes:todo"))
    p = tmp_path / "config.toml"
    p.write_text(f'[spool]\nhome = "{tmp_path / "home"}"\n[alerts]\ndry = true\n' + block
                 + f'\n[hermes]\nsubscriptions_path = "{tmp_path / "subs.json"}"\n')
    cfg = C.load_config(str(p))
    assert cfg["sources"]["tasks"]["kind"] == "poll"
