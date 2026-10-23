"""The gmail_delta / gcal_delta scripts in batch mode (T6): ``deploy/scripts/{hookspool_batch,gcal_delta,
gmail_delta}.py``, ported from the production scripts the retired bridges called in-process.

* ``hookspool_batch``: the document, emits, staging and the journal that makes a retry replay instead of
  losing a result;
* the scripts' behaviour against a scripted Google stub (``tests/_googlestub.py``);
* batch mode end to end through the real ``script`` destination and the dispatcher's fenced mark;
* parity with the originals in ``vps-src`` (skipped when absent): same stub, same inputs, same results and
  same state files;
* the committed scripts name no account, host or path of any deployment.
"""
from __future__ import annotations

import base64
import importlib
import io
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _googlestub  # noqa: E402
import vpssrc  # noqa: E402
from conftest import SENTINEL  # noqa: E402
from hookspool.destinations import build  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "deploy", "scripts")
OWNER = "owner@example.test"
AGENT = "agent@example.test"
UTC = timezone.utc


# ---- fixtures ---------------------------------------------------------------------------------------------
@pytest.fixture
def google():
    g = _googlestub.GoogleStub()
    yield g
    g.close()


def write_cred(path, port: int) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"client_id": "test-client", "client_secret": "test-client-secret",
                                "refresh_token": "test-refresh", "token_uri": f"http://127.0.0.1:{port}/token",
                                "access_token": "tok", "token": "tok", "expiry": "2999-01-01T00:00:00+00:00"}))
    os.chmod(path, 0o600)
    return str(path)


class Env:
    """A configured deployment of both scripts: temp state dir, credentials, stub URLs."""

    def __init__(self, tmp_path, google, **over):
        self.tmp, self.google = tmp_path, google
        self.state = str(tmp_path / "state")
        self.creds = tmp_path / "creds"
        write_cred(self.creds / f"{OWNER}.json", google.port)
        write_cred(self.creds / f"{AGENT}.json", google.port)
        self.vars = {"HOOKSPOOL_STATE_DIR": self.state, "GCAL_DELTA_CRED": str(self.creds / f"{OWNER}.json"),
                     "GCAL_DELTA_OWN": f"{OWNER},{AGENT}", "GMAIL_DELTA_OWNER": OWNER, "GMAIL_DELTA_AGENT": AGENT,
                     "GMAIL_DELTA_CRED_DIR": str(self.creds), "GCAL_DELTA_TZ": "America/Los_Angeles",
                     "GMAIL_DELTA_TZ": "America/Los_Angeles", "GMAIL_DELTA_OWN_DOMAINS": "ours.example.test",
                     **google.env()}
        self.vars.update(over)
        os.makedirs(self.state, exist_ok=True)

    def path(self, name: str) -> str:
        return os.path.join(self.state, name)


ORIG = vpssrc.names()                       # the private originals' deployment names, read from vps-src when present
MODULES = ("gcal_delta", "gmail_delta", "hookspool_batch", ORIG["sched_module"])


def _purge():
    for n in MODULES:
        sys.modules.pop(n, None)


@pytest.fixture
def scripts(tmp_path, google, monkeypatch):
    """``scripts(**env_overrides)`` -> (Env, gcal_delta, gmail_delta, hookspool_batch), freshly imported
    with the environment set (the scripts read it at import)."""
    monkeypatch.syspath_prepend(SCRIPTS)
    def clear():
        for k in list(os.environ):
            if k.startswith(("GCAL_DELTA_", "GMAIL_DELTA_", "HOOKSPOOL_DELTA")):
                monkeypatch.delenv(k)

    def make(**over):
        clear()
        env = Env(tmp_path, google, **over)
        for k, v in env.vars.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
            else:
                monkeypatch.setenv(k, v)
        _purge()
        g, m, hb = importlib.import_module("gcal_delta"), importlib.import_module("gmail_delta"), \
            importlib.import_module("hookspool_batch")
        return env, g, m, hb
    yield make
    _purge()


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


# ---- gcal_delta ----------------------------------------------------------------------------------------------
def hour(offset_h: float = 3.0) -> datetime:
    return datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=offset_h)


def gev(eid, start, minutes=30, summary="Standup", attendees=(), location="", etype=None, status=None, creator=None):
    ev = {"id": eid, "summary": summary, "status": status or "confirmed",
          "start": {"dateTime": start.isoformat()}, "end": {"dateTime": (start + timedelta(minutes=minutes)).isoformat()},
          "htmlLink": f"https://calendar.example.test/{eid}", "organizer": {"email": OWNER}}
    if attendees:
        ev["attendees"] = [{"email": a} for a in attendees]
    if location:
        ev["location"] = location
    if etype:
        ev["eventType"] = etype
    if creator:
        ev["creator"] = {"email": creator}
    return ev


def listing(*items, token="sync-next"):
    return {"items": list(items), "nextSyncToken": token}


def seed(g, items, token="sync-0", **extra):
    """State as left by a previous pass: the sync token and the signatures of ``items``."""
    st = {"syncToken": token, "last_seen": {e["id"]: g.signature(e) for e in items}, **extra}
    os.makedirs(os.path.dirname(g.STATE), exist_ok=True)
    with open(g.STATE, "w") as f:
        json.dump(st, f)


def state(g):
    with open(g.STATE) as f:
        return json.load(f)


def push(state="exists", n=1):
    return {"event_type": state, "channel_id": "c1", "resource_id": "r", "resource_state": state,
            "resource_uri": "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            "message_number": str(n), "source": "google-calendar"}


def test_no_state_directory_is_a_failure_not_a_crash(scripts):
    env, g, m, hb = scripts(HOOKSPOOL_STATE_DIR=None)
    with pytest.raises(g.Failed, match="state directory"):
        g.process(push())
    env, g, m, hb = scripts(GCAL_DELTA_CRED=None)
    with pytest.raises(g.Failed, match="GCAL_DELTA_CRED"):
        g.process(push())


def test_first_push_bootstraps_a_sync_token_and_says_nothing(scripts, google):
    env, g, m, hb = scripts()
    google.calendar_lists = [listing(gev("a", hour(1)), token="sync-1")]
    assert g.process(push()) is None
    st = state(g)
    assert st["syncToken"] == "sync-1" and list(st["last_seen"]) == ["a"]


def test_a_sync_ping_rebootstraps(scripts, google):
    env, g, m, hb = scripts()
    seed(g, [])
    google.calendar_lists = [listing(gev("a", hour(1)), token="sync-fresh")]
    assert g.process(push("sync")) is None
    assert state(g)["syncToken"] == "sync-fresh"


def test_410_gone_rebootstraps_quietly(scripts, google):
    env, g, m, hb = scripts()
    seed(g, [])
    google.calendar_lists = [410, listing(token="sync-new")]
    assert g.process(push()) is None and state(g)["syncToken"] == "sync-new"


def test_an_overlap_is_a_conflict_for_the_agent(scripts, google):
    env, g, m, hb = scripts()
    first = gev("a", hour(2), 60, "Planning")
    seed(g, [first])
    google.calendar_lists = [listing(gev("b", hour(2.5), 60, "Dentist"), token="s2")]
    out = g.process(push())
    assert len(out) == 1 and out[0]["event_type"] == "conflict" and out[0]["source"] == "google-calendar"
    assert "Dentist" in out[0]["conflicts_text"] and "Planning" in out[0]["conflicts_text"]
    assert out[0]["summary"].startswith("- NEW") and out[0]["actions_text"] == "nothing"
    st = state(g)
    assert st["syncToken"] == "s2" and set(st["last_seen"]) == {"a", "b"}


def test_a_back_to_back_pair_is_not_a_conflict_and_all_day_never_is(scripts, google):
    env, g, m, hb = scripts()
    seed(g, [gev("a", hour(2), 60)])
    allday = {"id": "d", "summary": "Holiday", "status": "confirmed", "start": {"date": hour(0).date().isoformat()},
              "end": {"date": (hour(0).date() + timedelta(days=1)).isoformat()}}
    google.calendar_lists = [listing(gev("b", hour(3), 30, "Next"), allday)]
    assert g.process(push()) is None


def test_noise_is_silent(scripts, google):
    env, g, m, hb = scripts()
    a = gev("a", hour(2), 60, "Planning")
    seed(g, [a])
    past = gev("p", hour(-30), 30, "Old")
    birthday = gev("bd", hour(4), 30, "Birthday", etype="birthday")
    same = dict(a, updated="2999-01-01T00:00:00Z", description="only the description changed")
    google.calendar_lists = [listing(past, birthday, same, token="s3")]
    assert g.process(push()) is None
    assert state(g)["syncToken"] == "s3", "silent passes still advance the token"


def test_a_moved_event_is_reported_and_its_travel_block_removed(scripts, google):
    env, g, m, hb = scripts()
    ev = gev("a", hour(5), 60, "Lunch", location="1 Example Street, Testville")
    block = gev("t", hour(4), 20, "\U0001f697 Travel → Lunch")
    seed(g, [ev, block])
    moved = gev("a", hour(6), 60, "Lunch", location="1 Example Street, Testville")
    google.calendar_lists = [listing(moved, token="s4")]
    assert g.process(push()) is None                          # moved, no conflict, no travel tool configured
    assert google.deleted == ["t"], "the stale travel block went with the moved event"


def test_an_external_attendee_asks_for_a_brief_only_when_a_brief_dir_is_set(scripts, google):
    ev = gev("m", hour(26), 30, "Intro call", attendees=("pat@outside.example.test", OWNER))
    env, g, m, hb = scripts()
    seed(g, [])
    google.calendar_lists = [listing(ev, token="s5")]
    assert g.process(push()) is None, "no GCAL_DELTA_BRIEF_DIR: no brief"

    briefs = str(env.tmp / "briefs")
    env, g, m, hb = scripts(GCAL_DELTA_BRIEF_DIR=briefs)
    seed(g, [])
    google.calendar_lists = [listing(ev, token="s5")]
    out = g.process(push())
    assert len(out) == 1 and out[0]["event_type"] == "brief" and out[0]["title"] == "Intro call"
    assert out[0]["attendees_text"] == "pat@outside.example.test" and out[0]["brief_path"].startswith(briefs + "/")
    assert out[0]["brief_path"].endswith("-m.md") or "intro-call" in out[0]["brief_path"]
    assert state(g)["last_seen"]["m"]["briefed"] is True
    req = json.load(open(os.path.join(env.state, "briefs_requested.json")))
    assert req["m"]["attempts"] == 1 and req["m"]["path"] == out[0]["brief_path"]
    seed(g, [ev], token="s5")                                 # an already-briefed event is not briefed again
    again = gev("m", hour(26), 30, "Intro call", attendees=("pat@outside.example.test", OWNER, "x@outside.example.test"))
    google.calendar_lists = [listing(again, token="s6")]
    st = state(g)
    st["last_seen"]["m"]["briefed"] = True
    json.dump(st, open(g.STATE, "w"))
    assert g.process(push()) is None


def test_the_travel_check_is_optional_and_creates_a_block_when_it_fits(scripts, google, tmp_path):
    day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    office_at, offsite_at = day.replace(hour=16), day.replace(hour=18)             # the same day in any US zone
    env0, g0, m0, hb0 = scripts()
    local = lambda t: g0.when({"start": {"dateTime": t.isoformat()}}, "start").isoformat()   # noqa: E731
    skill = tmp_path / "skill"
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts" / "travel_check.py").write_text(
        "import json\n"
        "print(json.dumps({'mode': 'driving', 'buffer_minutes': 10,\n"
        " 'events': [{'summary': 'Office', 'start': %r}, {'summary': 'Offsite', 'start': %r}],\n"
        " 'pairs': [{'from': 'Office', 'to': 'Offsite', 'action': 'travel_block_would_fit',\n"
        "  'travel_plus_buffer_min': 35, 'gap_min': 60, 'computed_minutes': 25}]}))\n" % (local(office_at), local(offsite_at)))
    env, g, m, hb = scripts(GCAL_DELTA_TRAVEL_PY=sys.executable, GCAL_DELTA_SKILL_DIR=str(skill))
    seed(g, [gev("o", office_at, 60, "Office")])
    google.calendar_lists = [listing(gev("x", offsite_at, 60, "Offsite", location="2 Example Road, Testville"), token="s7")]
    assert g.process(push()) is None
    assert len(google.created) == 1
    blk = google.created[0]
    assert blk["summary"] == "\U0001f697 Travel \u2192 Offsite" and blk["transparency"] == "transparent"
    assert blk["end"]["dateTime"] == local(offsite_at) and "computed by the travel check" in blk["description"]
    assert datetime.fromisoformat(blk["start"]["dateTime"]) == datetime.fromisoformat(local(offsite_at)) - timedelta(minutes=35)
    # without the tool configured nothing is attempted
    env, g, m, hb = scripts()
    google.created.clear()
    seed(g, [gev("o", office_at, 60, "Office")])
    google.calendar_lists = [listing(gev("x", offsite_at, 60, "Offsite", location="2 Example Road, Testville"), token="s7")]
    assert g.process(push()) is None and google.created == []


def test_a_failing_api_is_a_failure_and_leaves_the_state(scripts, google):
    env, g, m, hb = scripts()
    seed(g, [gev("a", hour(2))])
    before = state(g)
    google.calendar_lists = [500]
    with pytest.raises(g.Failed, match="events.list HTTP 500"):
        g.process(push())
    assert state(g) == before


def test_the_sync_command_is_started_after_a_routine_only_when_set(scripts, google, tmp_path):
    marker = tmp_path / "synced"
    sh = tmp_path / "sync.sh"
    sh.write_text(f"touch {marker}\n")
    env, g, m, hb = scripts(GCAL_DELTA_SYNC_SH=str(sh))
    seed(g, [gev("a", hour(2), 60, "Planning")])
    google.calendar_lists = [listing(gev("b", hour(2.5), 60, "Dentist"))]
    out = g.process(push())
    assert out[0]["actions_text"] == "event store refresh started"
    for _ in range(50):
        if marker.exists():
            break
        time.sleep(0.1)
    assert marker.exists()
    env, g, m, hb = scripts()
    seed(g, [gev("a", hour(2), 60, "Planning")])
    google.calendar_lists = [listing(gev("b", hour(2.5), 60, "Dentist"))]
    assert g.process(push())[0]["actions_text"] == "nothing"


# ---- gcal batch mode -----------------------------------------------------------------------------------------------
def batch(g, *pushes, bid="9", routes=None, monkeypatch=None):
    os.environ["HOOKSPOOL_BATCH_ID"] = bid
    os.environ["HOOKSPOOL_DELIVERY_ID"] = "hs_test_" + bid
    if routes is not None:
        os.environ["HOOKSPOOL_DELTA_ROUTES"] = json.dumps(routes)
    sys.stdin = io.StringIO(json.dumps(doc(*pushes)))
    out = io.StringIO()
    real, sys.stdout = sys.stdout, out
    try:
        rc = g.batch_main()
    finally:
        sys.stdout = real
    return rc, [json.loads(x)["enqueue"] for x in out.getvalue().splitlines()]


@pytest.fixture(autouse=True)
def _restore_stdin():
    stdin = sys.stdin
    yield
    sys.stdin = stdin
    for k in ("HOOKSPOOL_BATCH_ID", "HOOKSPOOL_DELIVERY_ID", "HOOKSPOOL_DELTA_ROUTES"):
        os.environ.pop(k, None)


def test_batch_mode_one_pass_for_many_pushes_emits_the_results(scripts, google):
    env, g, m, hb = scripts()
    seed(g, [gev("a", hour(2), 60, "Planning")])
    google.calendar_lists = [listing(gev("b", hour(2.5), 60, "Dentist"), token="s8")]
    rc, emits = batch(g, push(n=1), push(n=2), push(n=3), routes={"conflict": "hermes:calendar"})
    assert rc == 0 and google.count("GET", "/cal/events") == 1, "three pushes, one sync-token pass"
    assert len(emits) == 1
    e = emits[0]
    assert (e["source"], e["key"], e["action"], e["route"], e["event_type"]) == ("gcal", "conflict", "hermes:calendar",
                                                                              "calendar", "conflict")
    assert e["dedup_id"] == "hs_test_9:0" and (e["quiet_s"], e["max_s"]) == (0, 0)
    assert e["payload"]["event_type"] == "conflict" and "Dentist" in e["payload"]["conflicts_text"]
    assert state(g)["syncToken"] == "s8"


def test_batch_mode_default_destinations_are_hermes_event_types(scripts, google):
    env, g, m, hb = scripts()
    seed(g, [gev("a", hour(2), 60, "Planning")])
    google.calendar_lists = [listing(gev("b", hour(2.5), 60, "Dentist"))]
    rc, emits = batch(g, push())
    assert emits[0]["action"] == "hermes:conflict"


def test_batch_mode_prefers_a_real_push_over_a_sync_ping(scripts, google):
    env, g, m, hb = scripts()
    seed(g, [])
    google.calendar_lists = [listing(token="s9")]
    batch(g, push(n=1), push("sync", n=2))
    assert google.count("GET", "/cal/events") == 1 and "syncToken" in google.requests[-1]["query"]


def test_batch_mode_failure_exits_1_with_no_emits_and_no_state_change(scripts, google, capsys):
    env, g, m, hb = scripts()
    seed(g, [gev("a", hour(2), 60)])
    before = state(g)
    google.calendar_lists = [500]
    rc, emits = batch(g, push())
    assert rc == 1 and emits == [] and state(g) == before
    assert "events.list HTTP 500" in capsys.readouterr().err
    assert not os.path.exists(os.path.join(env.state, "journal")), "a failed pass leaves no journal"


def test_a_retry_after_a_crash_between_journal_and_state_loses_nothing(scripts, google, monkeypatch):
    """The reason for the journal: the pass read the calendar and would advance the sync token; the process
    dies before the token is saved. The retry must still report the conflict, from the journal."""
    env, g, m, hb = scripts()
    seed(g, [gev("a", hour(2), 60, "Planning")])
    before = state(g)
    google.calendar_lists = [listing(gev("b", hour(2.5), 60, "Dentist"), token="s10")]
    real = g._write_json

    def dying(path, obj, keep_mode=False):
        if path == g.STATE:
            raise OSError("killed while writing state")
        return real(path, obj, keep_mode)
    monkeypatch.setattr(g, "_write_json", dying)
    with pytest.raises(OSError):
        batch(g, push())
    assert state(g) == before, "the sync token did not move"
    monkeypatch.setattr(g, "_write_json", real)
    rc, emits = batch(g, push())
    assert rc == 0 and len(emits) == 1 and emits[0]["event_type"] == "conflict"
    assert google.count("GET", "/cal/events") == 1, "the retry did not read the calendar again"
    assert state(g)["syncToken"] == "s10"
    rc, again = batch(g, push())                                  # and a third attempt (lease lost after the mark)
    assert again == emits and google.count("GET", "/cal/events") == 1


def test_a_different_batch_runs_the_pass_for_real(scripts, google):
    env, g, m, hb = scripts()
    seed(g, [])
    google.calendar_lists = [listing(gev("a", hour(2), 60, "A"), gev("b", hour(2.5), 60, "B"), token="s11"),
                             listing(token="s12")]
    rc, e1 = batch(g, push(), bid="1")
    rc, e2 = batch(g, push(), bid="2")
    assert len(e1) == 1 and e2 == [] and state(g)["syncToken"] == "s12"


# ---- gmail_delta ------------------------------------------------------------------------------------------------------
def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def gmsg(mid, frm="Pat <pat@sender.example.test>", subject="Availability for a call?",
         text="Hi, are you available next week? What times work for you?", labels=("INBOX",), to=OWNER, cc=""):
    headers = [{"name": "From", "value": frm}, {"name": "To", "value": to}, {"name": "Subject", "value": subject},
               {"name": "Message-ID", "value": f"<{mid}@mail.example.test>"}]
    if cc:
        headers.append({"name": "Cc", "value": cc})
    return {"id": mid, "threadId": "t-" + mid, "labelIds": list(labels), "internalDate": str(int(time.time() * 1000)),
            "snippet": text[:40], "payload": {"mimeType": "multipart/alternative", "headers": headers,
                                              "parts": [{"mimeType": "text/plain", "body": {"data": b64url(text)}}]}}


def hist(*ids, history_id="120"):
    return {"history": [{"messagesAdded": [{"message": {"id": i}} for i in ids]}], "historyId": history_id}


def gpush(account=OWNER, history="110"):
    return {"emailAddress": account, "historyId": history}


def gstate(m, account=OWNER):
    with open(m.STATES[account]) as f:
        return json.load(f)


def gseed(m, account=OWNER, history="100", handled=()):
    os.makedirs(os.path.dirname(m.STATES[account]), exist_ok=True)
    with open(m.STATES[account], "w") as f:
        json.dump({"historyId": history, "handled": list(handled)}, f)


def email_check(tmp_path):
    p = tmp_path / "email_check.py"
    p.write_text('SKIP_DOMAINS = {"skipme.example.test"}\nSKIP_SENDERS = set()\nSKIP_KEYWORDS = ["newsletter", "sale"]\n'
                 'IMPORTANT_SENDERS = set()\nNOT_A_FILTER = 1\n')
    return str(p)


def test_a_push_for_an_unknown_mailbox_or_without_a_state_dir_is_handled(scripts, google):
    env, g, m, hb = scripts()
    assert m.process(gpush("stranger@example.test")) is None and google.requests == []
    env, g, m, hb = scripts(HOOKSPOOL_STATE_DIR=None)
    with pytest.raises(m.Failed, match="state directory"):
        m.process(gpush())


def test_first_push_bootstraps_the_history_id(scripts, google):
    env, g, m, hb = scripts()
    google.profile = {"historyId": "777"}
    assert m.process(gpush()) is None
    st = gstate(m)
    assert st["historyId"] == "777" and st["handled"] == [] and "bootstrapped" in st


def test_the_pubsub_envelope_is_decoded_like_the_bare_notification(scripts, google):
    env, g, m, hb = scripts()
    google.profile = {"historyId": "5"}
    env_ = {"message": {"data": base64.b64encode(json.dumps(gpush(AGENT)).encode()).decode()}}
    assert m.process(env_) is None
    assert os.path.exists(m.STATES[AGENT]) and not os.path.exists(m.STATES[OWNER])


def test_a_scheduling_request_gets_the_owners_free_windows(scripts, google):
    env, g, m, hb = scripts()
    gseed(m)
    google.history = [hist("m1")]
    google.messages["m1"] = gmsg("m1")
    out = m.process(gpush())
    assert len(out) == 1
    h = out[0]
    assert (h["event_type"], h["account"], h["source"], h["message_id"], h["thread_id"]) == \
           ("scheduling", OWNER, "gmail", "m1", "t-m1")
    assert h["from_address"] == "pat@sender.example.test" and h["from_domain"] == "sender.example.test"
    assert h["subject"] == "Availability for a call?" and h["rfc_message_id"] == "<m1@mail.example.test>"
    assert h["excerpt"].startswith("Hi, are you available") and "other_hits" not in h
    assert h["free_windows_text"].startswith("- ") or "no free working-hours window" in h["free_windows_text"]
    st = gstate(m)
    assert st["historyId"] == "120" and st["handled"] == ["m1"]
    body = json.loads([r for r in google.requests if r["path"] == "/freebusy"][0]["body"])
    assert body["timeZone"] == "America/Los_Angeles" and body["items"] == [{"id": "primary"}]


def test_a_message_that_is_not_a_scheduling_request_is_silent_but_advances_the_history(scripts, google):
    env, g, m, hb = scripts()
    gseed(m)
    google.history = [hist("m2")]
    google.messages["m2"] = gmsg("m2", subject="Hello", text="Just saying hi.")
    assert m.process(gpush()) is None
    assert gstate(m)["historyId"] == "120" and gstate(m)["handled"] == ["m2"]


def test_already_handled_ids_are_not_fetched_again(scripts, google):
    env, g, m, hb = scripts()
    gseed(m, handled=["m1"])
    google.history = [hist("m1", "m3")]
    google.messages["m3"] = gmsg("m3", subject="Hello", text="hi")
    m.process(gpush())
    fetched = [r["path"] for r in google.requests if r["path"].startswith("/gmail/messages/")]
    assert fetched == ["/gmail/messages/m3"]
    assert gstate(m)["handled"] == ["m1", "m1", "m3"], "ids are appended as read (the list is capped at 300)"


@pytest.mark.parametrize("label,msg", [
    ("promotional", dict(labels=("INBOX", "CATEGORY_PROMOTIONS"))),
    ("not inbox", dict(labels=("SENT",))),
    ("from the owner", dict(frm=OWNER)),
    ("from the agent", dict(frm=f"Agent <{AGENT}>")),
    ("from an own domain", dict(frm="Bot <bot@ours.example.test>")),
    ("no-reply", dict(frm="noreply@sender.example.test")),
    ("skip domain", dict(frm="x@skipme.example.test")),
    ("skip keyword", dict(subject="Big sale on availability")),
])
def test_filtered_mail_never_reaches_the_agent(scripts, google, tmp_path, label, msg):
    env, g, m, hb = scripts(GMAIL_DELTA_EMAIL_CHECK=email_check(tmp_path))
    gseed(m)
    google.history = [hist("m9")]
    google.messages["m9"] = gmsg("m9", **msg)
    assert m.process(gpush()) is None, label


def test_whole_word_keywords_and_the_recruiting_override(scripts, google, tmp_path):
    env, g, m, hb = scripts(GMAIL_DELTA_EMAIL_CHECK=email_check(tmp_path))
    gseed(m)
    google.history = [hist("a", "b")]
    google.messages["a"] = gmsg("a", subject="Salesforce availability", text="What times work for a call?")
    google.messages["b"] = gmsg("b", subject="Interview sale availability", text="What times work?")
    out = m.process(gpush())
    assert [h["message_id"] for h in out] == ["a", "b"], "'Salesforce' is not 'sale'; the recruiting word overrides 'sale'; both are reported"
    assert all(h["free_windows_text"] == out[0]["free_windows_text"] for h in out)
    assert google.count("POST", "/freebusy") == 1, "one free/busy read serves every request in the pass"


def test_mail_with_the_agent_on_the_thread_gets_no_draft_request(scripts, google):
    env, g, m, hb = scripts()
    gseed(m)
    google.history = [hist("m4")]
    google.messages["m4"] = gmsg("m4", cc=AGENT)
    assert m.process(gpush()) is None


def test_the_agents_own_mailbox_reports_its_mail(scripts, google):
    env, g, m, hb = scripts()
    gseed(m, AGENT)
    google.history = [hist("a1", "a2")]
    google.messages["a1"] = gmsg("a1", to=AGENT, subject="Receipt", text="thanks")
    google.messages["a2"] = gmsg("a2", to=AGENT, subject="Invoice", text="attached")
    out = m.process(gpush(AGENT))
    assert [h["event_type"] for h in out] == ["agent-mail", "agent-mail"] and out[0]["account"] == AGENT
    assert "free_windows_text" not in out[0] and gstate(m, AGENT)["handled"] == ["a1", "a2"]



def test_every_scheduling_request_in_one_pass_is_reported(scripts, google):
    """A coalesced batch can hold several requests; the first must not hide the rest."""
    env, g, m, hb = scripts()
    gseed(m)
    google.history = [hist("m1", "m2", history_id="130")]
    google.messages["m1"] = gmsg("m1")
    google.messages["m2"] = gmsg("m2", frm="Sam <sam@other.example.test>", subject="Times?", text="When are you free this week?")
    out = m.process(gpush())
    assert [h["message_id"] for h in out] == ["m1", "m2"] and [h["thread_id"] for h in out] == ["t-m1", "t-m2"]
    assert gstate(m)["handled"] == ["m1", "m2"], "both are handled because both were reported"


def test_the_agents_mailbox_reports_a_burst_of_any_size(scripts, google):
    env, g, m, hb = scripts()
    gseed(m, AGENT)
    ids = [f"b{i}" for i in range(9)]
    google.history = [hist(*ids, history_id="130")]
    for i in ids:
        google.messages[i] = gmsg(i, to=AGENT, subject=f"Note {i}", text="fyi")
    out = m.process(gpush(AGENT))
    assert [h["message_id"] for h in out] == ids and gstate(m, AGENT)["handled"] == ids


def test_the_scheduling_hand_off_module_is_optional(scripts, google):
    env, g, m, hb = scripts()
    gseed(m, AGENT)
    google.history = [hist("s1")]
    google.messages["s1"] = gmsg("s1", to=AGENT, subject="Re: meeting [sched]", text="Tuesday works")
    assert m.process(gpush(AGENT))[0]["event_type"] == "agent-mail", "no module: the generic path"

    env, g, m, hb = scripts(GMAIL_DELTA_SCHED_MODULE="_schedstub")
    gseed(m, AGENT)
    google.history = [hist("s1")]
    out = m.process(gpush(AGENT))
    assert len(out) == 1 and out[0]["event_type"] == "agent-schedule" and out[0]["kind"] == "reply"
    assert out[0]["tracked_thread_id"] == "tracked-1" and out[0]["meeting_subject"] == "Tracked meeting"
    assert out[0]["ref_tag"] == "[ref-tracked-1]" and out[0]["new_text"] == "Tuesday works"
    assert out[0]["free_windows_text"].startswith("- ") or "no free" in out[0]["free_windows_text"]


def test_a_history_gone_rebootstraps_and_other_errors_fail(scripts, google):
    env, g, m, hb = scripts()
    gseed(m)
    google.history = [404]
    google.profile = {"historyId": "900"}
    assert m.process(gpush()) is None and gstate(m)["historyId"] == "900"
    google.history = [500]
    with pytest.raises(m.Failed, match="history.list HTTP 500"):
        m.process(gpush())
    assert gstate(m)["historyId"] == "900"


def gbatch(m, *pushes, bid="3", routes=None):
    os.environ["HOOKSPOOL_BATCH_ID"] = bid
    os.environ["HOOKSPOOL_DELIVERY_ID"] = "hs_test_" + bid
    if routes is not None:
        os.environ["HOOKSPOOL_DELTA_ROUTES"] = json.dumps(routes)
    sys.stdin = io.StringIO(json.dumps(doc(*pushes)))
    out = io.StringIO()
    real, sys.stdout = sys.stdout, out
    try:
        rc = m.batch_main()
    finally:
        sys.stdout = real
    return rc, [json.loads(x)["enqueue"] for x in out.getvalue().splitlines()]


def test_batch_mode_reads_each_mailbox_once_and_keys_results_by_thread(scripts, google):
    env, g, m, hb = scripts()
    gseed(m)
    google.history = [hist("m1", history_id="130")]
    google.messages["m1"] = gmsg("m1")
    rc, emits = gbatch(m, gpush(history="111"), gpush(history="112"), gpush(history="113"),
                       routes={"scheduling": "hermes:mail"})
    assert rc == 0 and google.count("GET", "/gmail/history") == 1, "three pushes, one history read"
    assert len(emits) == 1
    e = emits[0]
    assert (e["source"], e["key"], e["action"], e["route"], e["event_type"]) == \
           ("gmail", f"{OWNER}:t-m1", "hermes:mail", "mail", "scheduling")
    assert e["dedup_id"] == "hs_test_3:0" and e["payload"]["message_id"] == "m1" and (e["quiet_s"], e["max_s"]) == (0, 0)
    assert gstate(m)["historyId"] == "130"


def test_batch_mode_emits_every_request_of_a_coalesced_batch(scripts, google):
    env, g, m, hb = scripts()
    gseed(m)
    google.history = [hist("m1", "m2", history_id="130")]
    google.messages["m1"] = gmsg("m1")
    google.messages["m2"] = gmsg("m2", frm="Sam <sam@other.example.test>", subject="Times?", text="When are you free?")
    rc, emits = gbatch(m, gpush(history="111"), gpush(history="112"))
    assert rc == 0 and [e["payload"]["message_id"] for e in emits] == ["m1", "m2"]
    assert [e["key"] for e in emits] == [f"{OWNER}:t-m1", f"{OWNER}:t-m2"], "each thread is its own Hermes event"
    assert [e["dedup_id"] for e in emits] == ["hs_test_3:0", "hs_test_3:1"]
    assert gstate(m)["handled"] == ["m1", "m2"]


def test_batch_mode_handles_two_mailboxes_in_one_batch(scripts, google):
    env, g, m, hb = scripts()
    gseed(m)
    gseed(m, AGENT)
    google.history = [hist("m1", history_id="131"), hist("a1", history_id="132")]
    google.messages["m1"] = gmsg("m1")
    google.messages["a1"] = gmsg("a1", to=AGENT, subject="Receipt", text="thanks")
    rc, emits = gbatch(m, gpush(OWNER), gpush(AGENT))
    assert rc == 0 and sorted(e["event_type"] for e in emits) == ["agent-mail", "scheduling"]
    assert sorted(e["dedup_id"] for e in emits) == ["hs_test_3:0", "hs_test_3:1"]


def test_batch_mode_failure_changes_nothing(scripts, google, capsys):
    env, g, m, hb = scripts()
    gseed(m)
    google.history = [500]
    rc, emits = gbatch(m, gpush())
    assert rc == 1 and emits == [] and gstate(m)["historyId"] == "100"
    assert "history.list HTTP 500" in capsys.readouterr().err


def test_gmail_retry_after_a_crash_replays_the_result_and_advances_once(scripts, google, monkeypatch):
    env, g, m, hb = scripts()
    gseed(m)
    google.history = [hist("m1", history_id="140")]
    google.messages["m1"] = gmsg("m1")
    real = m._write_json

    def dying(path, obj, keep_mode=False):
        if path == m.STATES[OWNER]:
            raise OSError("killed while writing state")
        return real(path, obj)
    monkeypatch.setattr(m, "_write_json", dying)
    with pytest.raises(OSError):
        gbatch(m, gpush())
    assert gstate(m)["historyId"] == "100"
    monkeypatch.setattr(m, "_write_json", real)
    rc, emits = gbatch(m, gpush())
    assert rc == 0 and len(emits) == 1 and gstate(m)["historyId"] == "140"
    assert google.count("GET", "/gmail/history") == 1, "the retry replayed the journal"


def test_replaying_an_old_unapplied_journal_does_not_move_the_state_back(scripts, google, monkeypatch, capsys):
    """The writer fails until the batch goes dead; a later batch is delivered; an operator then replays the old
    one. Its older history id and handled list must not overwrite the newer state."""
    env, g, m, hb = scripts()
    gseed(m)
    google.history = [hist("m1", history_id="140")]
    google.messages["m1"] = gmsg("m1")
    real = m._write_json
    monkeypatch.setattr(m, "_write_json", lambda path, obj, keep_mode=False: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        gbatch(m, gpush(), bid="old")
    monkeypatch.setattr(m, "_write_json", real)
    google.history = [hist("m5", history_id="150")]
    google.messages["m5"] = gmsg("m5", subject="Hello", text="hi")
    rc, emits = gbatch(m, gpush(), bid="newer")
    assert rc == 0 and emits == [] and gstate(m)["historyId"] == "150" and gstate(m)["handled"] == ["m5"]
    capsys.readouterr()
    rc, emits = gbatch(m, gpush(), bid="old")
    assert rc == 0 and [e["payload"]["message_id"] for e in emits] == ["m1"], "the old result is still replayed"
    assert gstate(m)["historyId"] == "150" and gstate(m)["handled"] == ["m5"], "the newer state is untouched"
    assert "not applying stale state" in capsys.readouterr().err


def test_a_journal_replay_after_a_partial_apply_does_not_rewrite_what_was_written(scripts, google, monkeypatch):
    env, g, m, hb = scripts()
    gseed(m)
    google.history = [hist("m1", history_id="140")]
    google.messages["m1"] = gmsg("m1")
    calls = []
    real = m._write_json

    def counting(path, obj, keep_mode=False):
        calls.append(path)
        return real(path, obj)
    monkeypatch.setattr(m, "_write_json", counting)
    assert gbatch(m, gpush(), bid="x")[0] == 0 and len(calls) == 1
    j = hb.load_journal(env.state, "x")
    j["applied"] = False                                       # as if killed after the write, before the flag
    hb._atomic_write(hb._journal_path(env.state, "x"), j)
    assert gbatch(m, gpush(), bid="x")[0] == 0 and len(calls) == 1, "already written: not written twice"


# ---- parity with the production originals (vps-src) ------------------------------------------------------------------
RENAMED = {"agent-mail": ORIG["mail_type"], "agent-schedule": ORIG["schedule_type"]}      # the originals' event types


def _walk(x, fn):
    if isinstance(x, dict):
        return {k: _walk(fn(k, v), fn) for k, v in x.items()}
    if isinstance(x, list):
        return [_walk(i, fn) for i in x]
    return x


def normal(x):
    """Drop what legitimately differs between two runs (timestamps taken at run time), map renamed names."""
    def f(k, v):
        if k in ("bootstrapped", "at"):
            return "<t>"
        if k == "event_type" and v in RENAMED:
            return RENAMED[v]
        return v
    return _walk(json.loads(json.dumps(x, default=str)), f)


def drop_other_hits(x):
    """The ports report every request of a pass instead of the first plus an ``other_hits`` count."""
    if isinstance(x, list):
        return [drop_other_hits(i) for i in x]
    if isinstance(x, dict):
        return {k: drop_other_hits(v) for k, v in x.items() if k != "other_hits"}
    return x


def calls_of(google):
    return [(r["method"], r["path"], sorted((k, v) for k, v in r["query"].items() if k != "timeMin"))
            for r in google.requests if r["path"] != "/token"]


def load_originals():
    root = vpssrc.root()
    if not root:
        pytest.skip("vps-src is not present in this checkout")
    d = os.path.join(root, "scripts")
    saved = {n: sys.modules.get(n) for n in MODULES}
    saved_path = list(sys.path)
    _purge()
    import _schedstub
    sys.modules[ORIG["sched_module"]] = _schedstub
    sys.path.insert(0, d)
    try:
        return importlib.import_module("gcal_delta"), importlib.import_module("gmail_delta")
    finally:
        sys.path[:] = saved_path
        _purge()
        for n, mod in saved.items():
            if mod is not None:
                sys.modules[n] = mod


class Orig:
    """The production modules, repointed at a temp deployment and a Google stub."""

    def __init__(self, tmp_path, google):
        self.g, self.m = load_originals()
        d = tmp_path / "orig"
        self.dir, self.google = str(d), google
        creds = d / "creds"
        write_cred(creds / f"{OWNER}.json", google.port)
        write_cred(creds / f"{AGENT}.json", google.port)
        os.makedirs(self.dir, exist_ok=True)
        g, m = self.g, self.m
        g.CRED, g.DATA, g.API = str(creds / f"{OWNER}.json"), self.dir, google.base + "/cal/events"
        g.STATE, g.LOG, g.ACTIONS = (os.path.join(self.dir, n) for n in ("gcal_push_state.json", "gcal_push.log", "push_actions.jsonl"))
        g.REQUESTED, g.PUBLISHED = (os.path.join(self.dir, n) for n in ("briefs_requested.json", "briefs_published.json"))
        g.SYNC_SH, g.BRIEF_DIR = "/nonexistent/sync.sh", str(tmp_path / "briefs")
        g.OWN = {OWNER, AGENT}
        m.ME, m.CRED_DIR, m.DATA = OWNER, str(creds), self.dir
        setattr(m, ORIG["agent_attr"], AGENT)
        m.STATE = os.path.join(self.dir, "gmail_push_state.json")
        m.STATES = {OWNER: m.STATE, AGENT: os.path.join(self.dir, ORIG["agent_state"])}
        m.LOG = os.path.join(self.dir, "gmail_push.log")
        m.GMAIL, m.FREEBUSY = google.base + "/gmail/", google.base + "/freebusy"
        m.EMAIL_CHECK = str(d / "email_check.py")
        (d / "email_check.py").write_text('SKIP_DOMAINS = {"skipme.example.test"}\nSKIP_SENDERS = set()\n'
                                           'SKIP_KEYWORDS = ["newsletter", "sale"]\n')


class New:
    def __init__(self, tmp_path, google, scripts):
        sub = tmp_path / "new"
        sub.mkdir()
        google2 = google
        self.env, self.g, self.m, self.hb = scripts(
            GCAL_DELTA_BRIEF_DIR=str(tmp_path / "briefs"), GCAL_DELTA_SYNC_SH="/nonexistent/sync.sh", GMAIL_DELTA_EMAIL_CHECK=str(sub / "email_check.py"),
            GMAIL_DELTA_SCHED_MODULE="_schedstub", GMAIL_DELTA_AGENT_STATE=ORIG["agent_state"],
            HOOKSPOOL_STATE_DIR=str(sub / "state"), GCAL_DELTA_OWN=f"{OWNER},{AGENT}", GMAIL_DELTA_OWN_DOMAINS=None)
        self.dir, self.google = str(sub / "state"), google2
        (sub / "email_check.py").write_text('SKIP_DOMAINS = {"skipme.example.test"}\nSKIP_SENDERS = set()\n'
                                             'SKIP_KEYWORDS = ["newsletter", "sale"]\n')


def snapshot(v):
    out = {}
    for name in sorted(os.listdir(v.dir)):
        p = os.path.join(v.dir, name)
        if name.endswith(".json") and os.path.isfile(p):
            out[name] = json.load(open(p))
    return out


def gcal_scenarios():
    # (name, seed items, listings, payload)
    a = lambda: gev("a", hour(2), 60, "Planning")                                              # noqa: E731
    return [
        ("bootstrap", [], None, [listing(gev("a", hour(1)), token="sync-1")], push()),
        ("sync ping", [], "s0", [listing(gev("a", hour(1)), token="sync-1")], push("sync")),
        ("gone", [], "s0", [410, listing(token="sync-2")], push()),
        ("conflict", [a()], "s0", [listing(gev("b", hour(2.5), 60, "Dentist"), token="s2")], push()),
        ("new external attendee", [], "s0", [listing(gev("m", hour(26), 30, "Intro call",
                                                         attendees=("pat@outside.example.test", OWNER)), token="s3")], push()),
        ("noise", [a()], "s0", [listing(gev("p", hour(-30), 30, "Old"), gev("bd", hour(4), 30, "Birthday", etype="birthday"),
                                        token="s4")], push()),
        ("moved and cancelled", [a(), gev("t", hour(1), 20, "\U0001f697 Travel → Planning")], "s0",
         [listing(gev("a", hour(6), 60, "Planning"), gev("zz", hour(9), 30, "Gone", status="cancelled"), token="s5")], push()),
        ("title and location change", [a()], "s0",
         [listing(dict(a(), summary="Planning v2", location="Somewhere"), token="s6")], push()),
    ]


@pytest.mark.parametrize("scenario", gcal_scenarios(), ids=[s[0] for s in gcal_scenarios()])
def test_gcal_delta_matches_the_production_script(scenario, tmp_path, scripts, google, monkeypatch):
    name, items, token, lists, payload = scenario
    google_b = _googlestub.GoogleStub()
    try:
        new = New(tmp_path, google, scripts)
        orig = Orig(tmp_path, google_b)
        results = []
        for v, gs in ((new, google), (orig, google_b)):
            gs.calendar_lists = [dict(x) if isinstance(x, dict) else x for x in lists]
            if token:
                seed(v.g, items, token=token)
            try:
                out = v.g.process(dict(payload))
            except v.g.Failed as e:
                out = f"Failed: {e}"
            results.append((out, snapshot(v), calls_of(gs)))
        assert normal(results[0]) == normal(results[1])
    finally:
        google_b.close()


def gmail_scenarios():
    return [
        ("bootstrap", None, [], {}, gpush()),
        ("scheduling request", "100", [hist("m1")], {"m1": gmsg("m1")}, gpush()),
        ("two requests", "100", [hist("m1", "m2")], {"m1": gmsg("m1"), "m2": gmsg("m2", subject="Times?", text="When are you free?")}, gpush()),
        ("not scheduling", "100", [hist("m2")], {"m2": gmsg("m2", subject="Hello", text="hi")}, gpush()),
        ("promotional", "100", [hist("m3")], {"m3": gmsg("m3", labels=("INBOX", "CATEGORY_PROMOTIONS"))}, gpush()),
        ("skip keyword", "100", [hist("m4")], {"m4": gmsg("m4", subject="Newsletter availability")}, gpush()),
        ("skip domain", "100", [hist("m5")], {"m5": gmsg("m5", frm="x@skipme.example.test")}, gpush()),
        ("agent cc'd", "100", [hist("m6")], {"m6": gmsg("m6", cc=AGENT)}, gpush()),
        ("history gone", "100", [404], {}, gpush()),
        ("agent mailbox", "100", [hist("a1", "a2")], {"a1": gmsg("a1", to=AGENT, subject="Receipt", text="ok"),
                                                      "a2": gmsg("a2", to=AGENT, subject="Re: meeting [sched]", text="Tuesday")},
         gpush(AGENT)),
        ("missing message", "100", [hist("gone1")], {}, gpush()),
        ("api error", "100", [500], {}, gpush()),
    ]


@pytest.mark.parametrize("scenario", gmail_scenarios(), ids=[s[0] for s in gmail_scenarios()])
def test_gmail_delta_matches_the_production_script(scenario, tmp_path, scripts, google):
    name, history, hists, messages, payload = scenario
    google_b = _googlestub.GoogleStub()
    try:
        new = New(tmp_path, google, scripts)
        orig = Orig(tmp_path, google_b)
        acct = payload["emailAddress"]
        results = []
        for v, gs in ((new, google), (orig, google_b)):
            gs.history, gs.messages, gs.profile = list(hists), dict(messages), {"historyId": "888"}
            gs.busy = [{"start": hour(30).isoformat(), "end": hour(31).isoformat()}]
            if history:
                gseed(v.m, acct, history)
            try:
                out = v.m.process(dict(payload))
            except v.m.Failed as e:
                out = f"Failed: {e}"
            results.append((drop_other_hits(out), snapshot(v), calls_of(gs)))
        if name == "two requests":                       # the one deliberate difference: the original dropped the second
            assert [h["message_id"] for h in results[0][0]] == ["m1", "m2"] and len(results[1][0]) == 1
            results[0] = (results[0][0][:1],) + results[0][1:]
        assert normal(results[0]) == normal(results[1])
    finally:
        google_b.close()


def test_the_production_originals_are_what_the_ports_were_made_from():
    """Guards the parity tests: they only mean something while the copies in vps-src still call the same
    functions, so a drift in the originals (or a rename in the ports) fails here first."""
    g, m = load_originals()
    for name in ("process", "signature", "api", "list_all", "access_token", "needs_brief", "brief_for", "retry_briefs"):
        assert callable(getattr(g, name)), name
    for name in ("process", "free_windows", "skip_reason", "body_text", "token_for"):
        assert callable(getattr(m, name)), name


# ---- through the real script destination and the dispatcher ---------------------------------------------------------------
def events_for(r, source):
    return [dict(x) for x in r.store._conn().execute(
        "SELECT e.id, e.event_type, e.dedup_id, b.key, b.destination, b.route FROM events e JOIN batches b "
        "ON b.id = e.batch_id WHERE b.source=? AND b.destination LIKE 'hermes:%' ORDER BY e.id", (source,))]


def write_env_file(path, env: Env, extra=None):
    keep = {k: v for k, v in env.vars.items() if k.startswith(("GCAL_DELTA_", "GMAIL_DELTA_"))}
    keep.update(extra or {})
    path.write_text("".join(f"{k}={v}\n" for k, v in keep.items()))
    return str(path)


def test_end_to_end_through_the_script_destination(rig, scripts, google, tmp_path, clock):
    """Pushes in the spool -> the dispatcher runs ``gcal_delta.py --batch`` -> the conflict it reports is
    enqueued for the Hermes route in the same transaction that marks the batch delivered."""
    env, g, m, hb = scripts()
    seed(g, [gev("a", hour(2), 60, "Planning")])
    google.calendar_lists = [listing(gev("b", hour(2.5), 60, "Dentist"), token="s20")]
    envfile = write_env_file(tmp_path / "gcal.env", env, {"HOOKSPOOL_DELTA_ROUTES": json.dumps({"conflict": "hermes:test-route"})})
    r = rig({"destinations": {"script:gcal_delta": {
        "argv": [sys.executable, os.path.join(SCRIPTS, "gcal_delta.py"), "--batch"], "cwd": SCRIPTS,
        "env_file": envfile, "state_dir": env.state, "timeout_s": 60}}}, destination_factory=build)
    ids = [r.store.enqueue("gcal", "primary", "script:gcal_delta", push(n=n), quiet_s=0, max_s=0).batch_id for n in (1, 2)]
    r.d.drain_once()
    b = r.batch(ids[0])
    assert b["state"] == "delivered", b
    assert [e["event_type"] for e in events_for(r, "gcal")] == ["conflict"]
    ev = events_for(r, "gcal")[0]
    assert ev["destination"] == "hermes:test-route" and ev["key"] == "conflict" and ev["route"] == "test-route"
    assert ev["dedup_id"].startswith("hs_") and ev["dedup_id"].endswith(f"_{b['id']}:0")
    assert os.path.isdir(os.path.join(env.state, "journal")), "HOOKSPOOL_STATE_DIR held the journal"
    sync_calls = google.count("GET", "/cal/events")
    assert sync_calls >= 1


def test_end_to_end_a_failing_pass_retries_and_reports_once(rig, scripts, google, tmp_path, clock):
    env, g, m, hb = scripts()
    seed(g, [gev("a", hour(2), 60, "Planning")])
    google.calendar_lists = [500, listing(gev("b", hour(2.5), 60, "Dentist"), token="s21")]
    envfile = write_env_file(tmp_path / "gcal.env", env, {"HOOKSPOOL_DELTA_ROUTES": json.dumps({"conflict": "hermes:test-route"})})
    r = rig({"destinations": {"script:gcal_delta": {
        "argv": [sys.executable, os.path.join(SCRIPTS, "gcal_delta.py"), "--batch"], "cwd": SCRIPTS,
        "env_file": envfile, "state_dir": env.state, "timeout_s": 60}}}, destination_factory=build)
    a = r.store.enqueue("gcal", "primary", "script:gcal_delta", push(), quiet_s=0, max_s=0)
    r.d.drain_once()
    b = r.batch(a.batch_id)
    assert b["state"] == "retry" and b["last_class"] == "batch" and "events.list HTTP 500" in (b["last_error"] or "")
    assert events_for(r, "gcal") == []
    clock.advance(60)
    r.d.drain_once()
    assert r.batch(a.batch_id)["state"] == "delivered" and len(events_for(r, "gcal")) == 1


def test_end_to_end_gmail(rig, scripts, google, tmp_path):
    env, g, m, hb = scripts()
    gseed(m)
    google.history = [hist("m1", history_id="150")]
    google.messages["m1"] = gmsg("m1")
    envfile = write_env_file(tmp_path / "gmail.env", env, {"HOOKSPOOL_DELTA_ROUTES": json.dumps({"scheduling": "hermes:test-route"})})
    r = rig({"destinations": {"script:gmail_delta": {
        "argv": [sys.executable, os.path.join(SCRIPTS, "gmail_delta.py"), "--batch"], "cwd": SCRIPTS,
        "env_file": envfile, "state_dir": env.state, "timeout_s": 60}}}, destination_factory=build)
    a = r.store.enqueue("gmail", OWNER, "script:gmail_delta", gpush(), quiet_s=0, max_s=0)
    r.d.drain_once()
    assert r.batch(a.batch_id)["state"] == "delivered"
    ev = events_for(r, "gmail")
    assert len(ev) == 1 and ev[0]["event_type"] == "scheduling" and ev[0]["destination"] == "hermes:test-route"
    assert ev[0]["key"] == f"{OWNER}:t-m1"
    assert gstate(m)["historyId"] == "150"


def test_the_script_environment_is_minimal_but_enough(rig, scripts, google, tmp_path):
    """The destination passes only PATH/HOME/LANG, the env file and HOOKSPOOL_*: the scripts must work on that."""
    env, g, m, hb = scripts()
    seed(g, [])
    google.calendar_lists = [listing(token="s30")]
    envfile = write_env_file(tmp_path / "gcal.env", env, {"GCAL_DELTA_DATA": env.state})
    r = rig({"destinations": {"script:gcal_delta": {
        "argv": [sys.executable, os.path.join(SCRIPTS, "gcal_delta.py"), "--batch"], "cwd": SCRIPTS,
        "env_file": envfile, "timeout_s": 60}}}, destination_factory=build)
    a = r.store.enqueue("gcal", "primary", "script:gcal_delta", push(), quiet_s=0, max_s=0)
    r.d.drain_once()
    assert r.batch(a.batch_id)["state"] == "delivered", "no state_dir on the destination: DATA from the env file is used"


# ---- the committed scripts are generic -------------------------------------------------------------------------------------
def test_the_committed_scripts_name_no_deployment():
    # any address that is not an example domain, deployment paths, agent-skill names, a webhook hostname
    banned = re.compile(r"(?i)(@(?!example\.|ours\.example|sender\.example)[\w-]+\.(com|net|org|io)\b|/root/|\.hermes|ocas-|"
                        r"los_angeles|webhook\.[a-z0-9-]+\.[a-z]+)")
    for name in sorted(os.listdir(SCRIPTS)):
        if not name.endswith((".py", ".md", ".sh")):
            continue
        with open(os.path.join(SCRIPTS, name), encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                assert not banned.search(line), f"{name}:{n}: {line.strip()[:100]}"


def test_the_scripts_are_stdlib_only_and_parse_on_the_minimum_python():
    import ast
    std = set(sys.stdlib_module_names) | {"hookspool_batch", "gcal_delta"}
    for name in ("hookspool_batch.py", "gcal_delta.py", "gmail_delta.py"):
        tree = ast.parse(open(os.path.join(SCRIPTS, name)).read(), feature_version=(3, 11))
        mods = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)} | \
               {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        assert mods <= std, (name, sorted(mods - std))


def test_batch_mode_is_documented_in_the_script_headers():
    for name in ("gcal_delta.py", "gmail_delta.py"):
        text = open(os.path.join(SCRIPTS, name)).read()
        assert "--batch" in text and "HOOKSPOOL_STATE_DIR" in text and "HOOKSPOOL_DELTA_ROUTES" in text
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "gcal_delta.py"), "--batch"], input="not json",
                       capture_output=True, text=True, env={"PATH": os.environ["PATH"]}, timeout=30)
    assert r.returncode == 1 and "batch document" in r.stderr
