"""The ``gcal`` bobbin's processor (ARCHITECTURE 14.3): the behaviour of the Calendar delta script it replaces, run
through the processor runtime (journal, staged state, emits) against a scripted Google (``tests/_googlestub.py``).

* sync token, bootstrap, ``410 Gone``, noise, conflicts, moved/cancelled events and their travel blocks;
* the optional travel hook (``hooks.travel``), meeting briefs, the ``changed`` output;
* failures: ``Retryable`` (exit 75) / ``Permanent`` (exit 65), and that a failed pass changes no state;
* the journal: a crash between the journal and the state write loses nothing, a replay does not read Calendar;
* end to end through the real ``script`` destination and the dispatcher's fenced mark.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import _bobbinrig
import _googlestub
from hookspool.bobbins.gcal import processor as gp
from hookspool.destinations import build

OWNER = "owner@example.test"
AGENT = "agent@example.test"
UTC = timezone.utc
TZ = "America/Los_Angeles"
CFG = SimpleNamespace(tz=ZoneInfo(TZ), own={OWNER, AGENT})
OUTPUTS = ("conflict", "brief", "changed")


# ---- fixtures ---------------------------------------------------------------------------------------------
@pytest.fixture
def google():
    g = _googlestub.GoogleStub()
    yield g
    g.close()


@pytest.fixture
def make(tmp_path, google):
    """``make(hooks=..., **settings)`` -> a Rig for the gcal bobbin."""
    def build_rig(hooks=None, **over):
        cred = _bobbinrig.write_cred(tmp_path / "creds" / "cal.json", google.port)
        settings = {"tz": TZ, "own": [OWNER, AGENT], **google.gcal_settings(), **over}
        return _bobbinrig.Rig(tmp_path, "gcal", OUTPUTS, settings, {"cred": cred}, hooks=hooks)
    return build_rig


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


def seed(rig, items, token="sync-0", **extra):
    """State as left by a previous pass: the sync token and the signatures of ``items``."""
    rig.seed_state(gp.STATE, {"syncToken": token, "last_seen": {e["id"]: gp.signature(e, CFG) for e in items}, **extra})


def push(state="exists", n=1):
    return {"event_type": state, "channel_id": "c1", "resource_id": "r", "resource_state": state,
            "resource_uri": "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            "message_number": str(n), "source": "google-calendar"}


def only(items, event_type):
    return [i for i in items if i["event_type"] == event_type]


def kinds(items):
    return [i["event_type"] for i in items]


# ---- the sync-token loop -----------------------------------------------------------------------------------
def test_first_push_bootstraps_a_sync_token_and_says_nothing(make, google):
    rig = make()
    google.calendar_lists = [listing(gev("a", hour(1)), token="sync-1")]
    assert rig.items(push()) == []
    st = rig.get_state(gp.STATE)
    assert st["syncToken"] == "sync-1" and list(st["last_seen"]) == ["a"] and "bootstrapped" in st


def test_a_sync_ping_rebootstraps(make, google):
    rig = make()
    seed(rig, [])
    google.calendar_lists = [listing(gev("a", hour(1)), token="sync-fresh")]
    assert rig.items(push("sync")) == []
    assert rig.get_state(gp.STATE)["syncToken"] == "sync-fresh"


def test_410_gone_rebootstraps_quietly(make, google):
    rig = make()
    seed(rig, [])
    google.calendar_lists = [410, listing(token="sync-new")]
    assert rig.items(push()) == [] and rig.get_state(gp.STATE)["syncToken"] == "sync-new"


def test_a_bootstrap_without_a_sync_token_is_retried(make, google):
    rig = make()
    google.calendar_lists = [{"items": []}]
    rc, emits, err = rig.run(push())
    assert rc == 75 and emits == [] and not rig.has_state(gp.STATE)


def test_events_are_read_across_pages(make, google):
    rig = make()
    seed(rig, [])
    google.calendar_lists = [{"items": [gev("a", hour(2), 60, "A")], "nextPageToken": "p2"},
                             {"items": [gev("b", hour(2.5), 60, "B")], "nextSyncToken": "s-pages"}]
    out = rig.items(push())
    assert kinds(out) == ["conflict", "changed"]
    assert rig.get_state(gp.STATE)["syncToken"] == "s-pages"
    assert google.requests[-1]["query"].get("pageToken") == "p2"


# ---- conflicts and noise -----------------------------------------------------------------------------------
def test_an_overlap_is_a_conflict_for_the_agent(make, google):
    rig = make()
    seed(rig, [gev("a", hour(2), 60, "Planning")])
    google.calendar_lists = [listing(gev("b", hour(2.5), 60, "Dentist"), token="s2")]
    out = rig.items(push())
    c = only(out, "conflict")
    assert len(c) == 1 and c[0]["source"] == "google-calendar"
    assert "Dentist" in c[0]["conflicts_text"] and "Planning" in c[0]["conflicts_text"]
    assert c[0]["summary"].startswith("- NEW") and c[0]["actions_text"] == "nothing"
    st = rig.get_state(gp.STATE)
    assert st["syncToken"] == "s2" and set(st["last_seen"]) == {"a", "b"}


def test_a_back_to_back_pair_is_not_a_conflict_and_all_day_never_is(make, google):
    rig = make()
    seed(rig, [gev("a", hour(2), 60)])
    allday = {"id": "d", "summary": "Holiday", "status": "confirmed", "start": {"date": hour(0).date().isoformat()},
              "end": {"date": (hour(0).date() + timedelta(days=1)).isoformat()}}
    google.calendar_lists = [listing(gev("b", hour(3), 30, "Next"), allday)]
    assert only(rig.items(push()), "conflict") == []


def test_noise_is_silent(make, google):
    rig = make()
    a = gev("a", hour(2), 60, "Planning")
    seed(rig, [a])
    past = gev("p", hour(-30), 30, "Old")
    birthday = gev("bd", hour(4), 30, "Birthday", etype="birthday")
    same = dict(a, updated="2999-01-01T00:00:00Z", description="only the description changed")
    google.calendar_lists = [listing(past, birthday, same, token="s3")]
    assert rig.items(push()) == [], "no real change: nothing, not even a changed notice"
    assert rig.get_state(gp.STATE)["syncToken"] == "s3", "silent passes still advance the token"


def test_a_moved_event_is_reported_and_its_travel_block_removed(make, google):
    rig = make()
    ev = gev("a", hour(5), 60, "Lunch", location="1 Example Street, Testville")
    block = gev("t", hour(4), 20, "\U0001f697 Travel → Lunch")
    seed(rig, [ev, block])
    moved = gev("a", hour(6), 60, "Lunch", location="1 Example Street, Testville")
    google.calendar_lists = [listing(moved, token="s4")]
    out = rig.items(push())
    assert only(out, "conflict") == []                       # moved, no conflict, no travel hook
    assert google.deleted == ["t"], "the stale travel block went with the moved event"
    ch = only(out, "changed")[0]
    assert ch["kinds"] == ["moved"] and ch["event_ids"] == ["a"]


def test_a_cancelled_event_is_a_change(make, google):
    rig = make()
    seed(rig, [gev("a", hour(5), 60, "Lunch")])
    google.calendar_lists = [listing(gev("a", hour(5), 60, "Lunch", status="cancelled"), token="s5c")]
    ch = only(rig.items(push()), "changed")
    assert len(ch) == 1 and ch[0]["kinds"] == ["cancelled"]
    assert "a" not in rig.get_state(gp.STATE)["last_seen"]


# ---- the changed output ------------------------------------------------------------------------------------
def test_changed_is_one_per_pass_with_the_event_ids_and_dates(make, google):
    rig = make()
    seed(rig, [])
    first, second = gev("e1", hour(26), 30, "One"), gev("e2", hour(50), 30, "Two")
    google.calendar_lists = [listing(first, second, token="s6")]
    out = rig.items(push(n=1), push(n=2))
    ch = only(out, "changed")
    assert len(ch) == 1
    c = ch[0]
    assert c["source"] == "google-calendar" and c["key"] == "changed" and c["count"] == 2
    assert c["kinds"] == ["new", "new"] and c["event_ids"] == ["e1", "e2"]
    want = sorted({gp.signature(e, CFG)["start"][:10] for e in (first, second)})
    assert c["dates"] == want


def test_changed_follows_the_other_outputs_and_an_unmapped_output_is_dropped(make, google):
    rig = make()
    seed(rig, [gev("a", hour(2), 60, "Planning")])
    google.calendar_lists = [listing(gev("b", hour(2.5), 60, "Dentist"), token="s7")]
    rc, emits, err = rig.run(push(), routes={"conflict": "hermes:calendar"})
    assert rc == 0 and [e["event_type"] for e in emits] == ["conflict"]
    assert "dropped 1 item(s) of unmapped event types: changed x1" in err


# ---- the travel hook ---------------------------------------------------------------------------------------
TRAVEL_HOOK = """
calls = []

def travel(event, day_events, ctx):
    calls.append(event["id"])
    if event["summary"] != "Offsite":
        return None
    prev = [v for v in day_events.values() if v["summary"] == "Office"]
    if not prev:
        return {"note": "no origin"}
    return {"block": {"minutes": 35, "from": "Office", "computed_minutes": 25, "buffer_minutes": 10}}
"""


def travel_day():
    day = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return day.replace(hour=16), day.replace(hour=18)             # the same day in any US zone


def local(t) -> str:
    return gp.when({"start": {"dateTime": t.isoformat()}}, "start", CFG.tz).isoformat()


def test_the_travel_hook_creates_a_block_when_it_says_one_fits(make, google):
    office_at, offsite_at = travel_day()
    rig = make(hooks=TRAVEL_HOOK)
    seed(rig, [gev("o", office_at, 60, "Office")])
    google.calendar_lists = [listing(gev("x", offsite_at, 60, "Offsite", location="2 Example Road, Testville"), token="s8")]
    out = rig.items(push())
    assert len(google.created) == 1
    blk = google.created[0]
    assert blk["summary"] == "\U0001f697 Travel → Offsite" and blk["transparency"] == "transparent"
    assert blk["end"]["dateTime"] == local(offsite_at) and "computed by the travel check" in blk["description"]
    assert 'from "Office"' in blk["description"]
    assert datetime.fromisoformat(blk["start"]["dateTime"]) == datetime.fromisoformat(local(offsite_at)) - timedelta(minutes=35)
    assert only(out, "changed")[0]["event_ids"] == ["x"]


def test_without_a_travel_hook_nothing_is_attempted(make, google):
    office_at, offsite_at = travel_day()
    rig = make()
    seed(rig, [gev("o", office_at, 60, "Office")])
    google.calendar_lists = [listing(gev("x", offsite_at, 60, "Offsite", location="2 Example Road, Testville"), token="s8")]
    rig.items(push())
    assert google.created == []


def test_the_travel_hook_is_asked_only_about_changed_events_with_an_address(make, google):
    office_at, offsite_at = travel_day()
    rig = make(hooks=TRAVEL_HOOK)
    seed(rig, [gev("o", office_at, 60, "Office")])
    google.calendar_lists = [listing(gev("x", offsite_at, 60, "Offsite", location="2 Example Road, Testville"),
                                     gev("v", offsite_at + timedelta(hours=3), 30, "Call", location="Zoom"),
                                     gev("n", offsite_at + timedelta(hours=4), 30, "Nowhere"), token="s9")]
    rig.items(push())
    assert sys.modules["hookspool._hooks_gcal"].calls == ["x"], "not the office (unchanged), not 'Zoom', not no location"
    assert len(google.created) == 1


def test_a_hook_conflict_and_note_reach_the_report_and_a_failing_hook_does_not_stop_the_pass(make, google):
    office_at, offsite_at = travel_day()
    hook = """
def travel(event, day_events, ctx):
    if event["summary"] == "Offsite":
        return {"conflict": 'not enough time to get from "Office" to "Offsite": needs 35 min, gap is 10 min',
                "note": "travel check for the day: degraded (no block created)"}
    raise RuntimeError("boom")
"""
    rig = make(hooks=hook)
    seed(rig, [gev("o", office_at, 60, "Office")])
    google.calendar_lists = [listing(gev("x", offsite_at, 60, "Offsite", location="2 Example Road, Testville"),
                                     gev("y", offsite_at + timedelta(hours=3), 30, "Other", location="3 Example Road, Testville"),
                                     token="s10")]
    out = rig.items(push())
    c = only(out, "conflict")[0]
    assert "needs 35 min, gap is 10 min" in c["conflicts_text"]
    assert "degraded (no block created)" in c["actions_text"] and "failed (no block created)" in c["actions_text"]
    assert google.created == []


def test_a_block_that_already_exists_is_not_created_again(make, google):
    office_at, offsite_at = travel_day()
    rig = make(hooks=TRAVEL_HOOK)
    seed(rig, [gev("o", office_at, 60, "Office"), gev("t", offsite_at - timedelta(minutes=35), 35, "\U0001f697 Travel → Offsite")])
    google.calendar_lists = [listing(gev("x", offsite_at, 60, "Offsite", location="2 Example Road, Testville"), token="s11")]
    rig.items(push())
    assert google.created == []


def test_a_failing_block_insert_is_reported_not_fatal(make, google):
    office_at, offsite_at = travel_day()
    rig = make(hooks=TRAVEL_HOOK)
    seed(rig, [gev("o", office_at, 60, "Office"), gev("z", offsite_at, 30, "Clash")])
    google.calendar_lists = [listing(gev("x", offsite_at, 60, "Offsite", location="2 Example Road, Testville"), token="s12")]
    google.fail_insert = True
    out = rig.items(push())
    assert 'could not create' in only(out, "conflict")[0]["actions_text"]


# ---- briefs ------------------------------------------------------------------------------------------------
def test_an_external_attendee_asks_for_a_brief_only_when_a_brief_dir_is_set(make, google, tmp_path):
    ev = gev("m", hour(26), 30, "Intro call", attendees=("pat@outside.example.test", OWNER))
    rig = make()
    seed(rig, [])
    google.calendar_lists = [listing(ev, token="s13")]
    assert only(rig.items(push()), "brief") == [], "no brief_dir: no brief"

    briefs = str(tmp_path / "briefs")
    rig = make(brief_dir=briefs)
    seed(rig, [])
    google.calendar_lists = [listing(ev, token="s13")]
    out = rig.items(push(), bid="b2")
    b = only(out, "brief")
    assert len(b) == 1 and b[0]["title"] == "Intro call" and b[0]["key"] == "brief:m"
    assert b[0]["attendees_text"] == "pat@outside.example.test" and b[0]["brief_path"].startswith(briefs + "/")
    assert "intro-call" in b[0]["brief_path"] and b[0]["event_id"] == "m"
    st = rig.get_state(gp.STATE)
    assert st["last_seen"]["m"]["briefed"] is True and "_id" not in st["last_seen"]["m"]
    req = rig.get_state(gp.REQUESTED)
    assert req["m"]["attempts"] == 1 and req["m"]["path"] == b[0]["brief_path"]
    # an already-briefed event is not briefed again
    again = gev("m", hour(26), 30, "Intro call", attendees=("pat@outside.example.test", OWNER, "x@outside.example.test"))
    google.calendar_lists = [listing(again, token="s14")]
    assert only(rig.items(push(), bid="b3"), "brief") == []


def test_a_brief_that_never_appeared_is_requested_again_later(make, google, tmp_path):
    briefs = str(tmp_path / "briefs")
    rig = make(brief_dir=briefs)
    ev = gev("m", hour(26), 30, "Intro call", attendees=("pat@outside.example.test", OWNER))
    seed(rig, [ev])
    st = rig.get_state(gp.STATE)
    st["last_seen"]["m"]["briefed"] = True
    rig.seed_state(gp.STATE, st)
    old = (datetime.now(CFG.tz) - timedelta(hours=7)).isoformat()
    rig.seed_state(gp.REQUESTED, {"m": {"path": briefs + "/nope.md", "at": old, "attempts": 1, "title": "Intro call"}})
    google.calendar_lists = [listing(token="s15")]
    out = rig.items(push())
    assert kinds(out) == ["brief"] and out[0]["event_id"] == "m"
    assert rig.get_state(gp.REQUESTED)["m"]["attempts"] == 2
    # published briefs, existing files and exhausted attempts are left alone
    rig.seed_state(gp.PUBLISHED, {"m": True})
    google.calendar_lists = [listing(token="s16")]
    assert rig.items(push(), bid="b4") == []


# ---- failures ----------------------------------------------------------------------------------------------
def test_a_server_error_is_a_retry_and_leaves_the_state(make, google):
    rig = make()
    seed(rig, [gev("a", hour(2))])
    before = rig.get_state(gp.STATE)
    google.calendar_lists = [500]
    rc, emits, err = rig.run(push())
    assert rc == 75 and emits == [] and rig.get_state(gp.STATE) == before
    assert "will be retried" in err and not os.path.exists(rig.state / "journal"), "a failed pass leaves no journal"


def test_a_rejected_request_is_permanent(make, google):
    rig = make()
    seed(rig, [gev("a", hour(2))])
    google.calendar_lists = [403]
    rc, emits, err = rig.run(push())
    assert rc == 65 and emits == [] and "permanent failure" in err


def test_a_missing_credential_is_permanent_and_an_unknown_zone_too(make, google, tmp_path):
    rig = make()
    rig.env["HOOKSPOOL_SECRET_REFS"] = "{}"
    rc, emits, err = rig.run(push())
    assert rc == 65
    rig = make(tz="Nowhere/Land")
    rc, emits, err = rig.run(push())
    assert rc == 65 and "tz setting" in err


def test_the_credential_file_is_never_rewritten_and_the_token_is_cached(make, google, tmp_path):
    rig = make()
    cred = tmp_path / "creds" / "cal.json"
    before = cred.read_text()
    seed(rig, [])
    google.calendar_lists = [listing(token="s17"), listing(token="s18")]
    rig.items(push())
    rig.items(push(), bid="10")
    assert cred.read_text() == before
    assert google.token_calls == 1, "the access token is cached in the runtime state across batches"
    assert all(r["auth"] == "Bearer tok-refreshed" for r in google.requests if r["path"].startswith("/cal"))


# ---- batches -----------------------------------------------------------------------------------------------
def test_many_pushes_are_one_pass_and_emit_to_the_mapped_destinations(make, google):
    rig = make()
    seed(rig, [gev("a", hour(2), 60, "Planning")])
    google.calendar_lists = [listing(gev("b", hour(2.5), 60, "Dentist"), token="s19")]
    rc, emits, err = rig.run(push(n=1), push(n=2), push(n=3), routes={"conflict": "hermes:calendar", "changed": "script:refresh"})
    assert rc == 0 and google.count("GET", "/cal/events") == 1, "three pushes, one sync-token pass"
    assert [e["event_type"] for e in emits] == ["conflict", "changed"]
    e = emits[0]
    assert (e["source"], e["key"], e["action"], e["route"]) == ("gcal", "conflict", "hermes:calendar", "calendar")
    assert e["dedup_id"] == "hs_test_9:0" and (e["quiet_s"], e["max_s"]) == (0, 0)
    assert "Dentist" in e["payload"]["conflicts_text"]
    assert (emits[1]["action"], emits[1]["dedup_id"], emits[1]["key"]) == ("script:refresh", "hs_test_9:1", "changed")
    assert rig.get_state(gp.STATE)["syncToken"] == "s19"


def test_a_real_push_wins_over_a_sync_ping(make, google):
    rig = make()
    seed(rig, [])
    google.calendar_lists = [listing(token="s20")]
    rig.items(push(n=1), push("sync", n=2))
    assert google.count("GET", "/cal/events") == 1 and "syncToken" in google.requests[-1]["query"]


def test_a_retry_after_a_crash_between_journal_and_state_loses_nothing(make, google, monkeypatch):
    """The reason for the journal: the pass read the calendar and would advance the sync token; the process dies
    before the token is saved. The retry must still report the conflict, from the journal."""
    rig = make()
    seed(rig, [gev("a", hour(2), 60, "Planning")])
    before = rig.get_state(gp.STATE)
    google.calendar_lists = [listing(gev("b", hour(2.5), 60, "Dentist"), token="s21")]
    real = _bobbinrig.P._atomic_write
    state_path = rig.path(gp.STATE)

    def dying(path, obj):
        if path == state_path:
            raise OSError("killed while writing state")
        return real(path, obj)
    monkeypatch.setattr(_bobbinrig.P, "_atomic_write", dying)
    rc, emits, err = rig.run(push())
    assert rc == 75 and emits == [], "the apply failed after the journal was written"
    assert rig.get_state(gp.STATE) == before, "the sync token did not move"
    assert rig.journal() is not None
    monkeypatch.setattr(_bobbinrig.P, "_atomic_write", real)
    rc, emits, err = rig.run(push())
    assert rc == 0 and kinds([e["payload"] for e in emits]) == ["conflict", "changed"]
    assert google.count("GET", "/cal/events") == 1, "the retry did not read the calendar again"
    assert rig.get_state(gp.STATE)["syncToken"] == "s21"
    rc, again, err = rig.run(push())                              # and a third attempt (lease lost after the mark)
    assert [e["payload"] for e in again] == [e["payload"] for e in emits] and google.count("GET", "/cal/events") == 1


def test_a_different_batch_runs_the_pass_for_real(make, google):
    rig = make()
    seed(rig, [])
    google.calendar_lists = [listing(gev("a", hour(2), 60, "A"), gev("b", hour(2.5), 60, "B"), token="s22"),
                             listing(token="s23")]
    e1 = rig.items(push(), bid="1")
    e2 = rig.items(push(), bid="2")
    assert "conflict" in kinds(e1) and e2 == [] and rig.get_state(gp.STATE)["syncToken"] == "s23"


# ---- end to end: the real script destination and the dispatcher ---------------------------------------------
def events_for(r, source):
    return [dict(x) for x in r.store._conn().execute(
        "SELECT e.id, e.event_type, e.dedup_id, b.key, b.destination, b.route FROM events e JOIN batches b "
        "ON b.id = e.batch_id WHERE b.source=? AND b.destination LIKE 'hermes:%' ORDER BY e.id", (source,))]


def processor_destination(rig, tmp_path, name="script:cal", **extra):
    """The destination an expansion builds, with the interpreter line spelled out for a checkout that is not
    installed (the dispatcher uses ``-I``, which ignores the working directory)."""
    envfile = tmp_path / "proc.env"
    envfile.write_text("".join(f"{k}={v}\n" for k, v in rig.env.items()
                               if k not in ("HOOKSPOOL_STATE_DIR", "HOOKSPOOL_BATCH_ID", "HOOKSPOOL_DELIVERY_ID")))
    return {name: {"argv": [sys.executable, "-m", "hookspool.processor", rig.instance], "cwd": _bobbinrig.ROOT,
                   "env_file": str(envfile), "state_dir": str(rig.state), "timeout_s": 60, "max_inflight": 1, **extra}}


def test_end_to_end_through_the_script_destination(rig, make, google, tmp_path):
    """A push in the spool -> the dispatcher runs the processor -> the conflict it reports is enqueued for the
    Hermes route in the same transaction that marks the batch delivered."""
    pr = make()
    pr.env["HOOKSPOOL_DELTA_ROUTES"] = json.dumps({"conflict": "hermes:test-route", "changed": "hermes:test-changed"})
    seed(pr, [gev("a", hour(2), 60, "Planning")])
    google.calendar_lists = [listing(gev("b", hour(2.5), 60, "Dentist"), token="s30")]
    r = rig({"destinations": processor_destination(pr, tmp_path)}, destination_factory=build)
    ids = [r.store.enqueue("gcal", "primary", "script:cal", push(n=n), quiet_s=0, max_s=0).batch_id for n in (1, 2)]
    r.d.drain_once()
    b = r.batch(ids[0])
    assert b["state"] == "delivered", b
    got = events_for(r, "gcal")
    assert sorted(e["event_type"] for e in got) == ["changed", "conflict"]
    ev = next(e for e in got if e["event_type"] == "conflict")
    assert ev["destination"] == "hermes:test-route" and ev["key"] == "conflict" and ev["route"] == "test-route"
    assert ev["dedup_id"].startswith("hs_") and ev["dedup_id"].endswith(f"_{b['id']}:0")
    assert os.path.isdir(str(pr.state / "journal")), "HOOKSPOOL_STATE_DIR held the journal"
    assert (pr.state / ".runtime.json").stat().st_mode & 0o077 == 0, "cached tokens are private"


def test_end_to_end_a_failing_pass_retries_and_reports_once(rig, make, google, tmp_path, clock):
    pr = make()
    pr.env["HOOKSPOOL_DELTA_ROUTES"] = json.dumps({"conflict": "hermes:test-route"})
    seed(pr, [gev("a", hour(2), 60, "Planning")])
    google.calendar_lists = [500, listing(gev("b", hour(2.5), 60, "Dentist"), token="s31")]
    r = rig({"destinations": processor_destination(pr, tmp_path)}, destination_factory=build)
    a = r.store.enqueue("gcal", "primary", "script:cal", push(), quiet_s=0, max_s=0)
    r.d.drain_once()
    b = r.batch(a.batch_id)
    assert b["state"] == "retry" and b["last_class"] == "batch" and b["last_error"].startswith("exit 75")
    assert events_for(r, "gcal") == []
    clock.advance(60)
    r.d.drain_once()
    assert r.batch(a.batch_id)["state"] == "delivered" and len(events_for(r, "gcal")) == 1


def test_end_to_end_a_permanent_failure_dead_letters_on_the_first_attempt(rig, make, google, tmp_path):
    pr = make()
    seed(pr, [gev("a", hour(2), 60, "Planning")])
    google.calendar_lists = [403]
    r = rig({"destinations": processor_destination(pr, tmp_path)}, destination_factory=build)
    a = r.store.enqueue("gcal", "primary", "script:cal", push(), quiet_s=0, max_s=0)
    r.d.drain_once()
    b = r.batch(a.batch_id)
    assert b["state"] == "dead" and b["last_class"] == "permanent", b
    assert "exit 65" in (b["last_error"] or "") and "permanent failure" in b["last_error"]
    r.d.drain_once()
    assert r.batch(a.batch_id)["state"] == "dead" and google.count("GET", "/cal/events") == 1, "one attempt, no retry"


def test_bootstrap_and_state_survive_the_dispatcher_environment(rig, make, google, tmp_path):
    """The destination passes only PATH/HOME/LANG, the env file and HOOKSPOOL_*: the processor works on that."""
    pr = make()
    seed(pr, [])
    google.calendar_lists = [listing(token="s32")]
    r = rig({"destinations": processor_destination(pr, tmp_path)}, destination_factory=build)
    a = r.store.enqueue("gcal", "primary", "script:cal", push(), quiet_s=0, max_s=0)
    r.d.drain_once()
    assert r.batch(a.batch_id)["state"] == "delivered"
    assert pr.get_state(gp.STATE)["syncToken"] == "s32"
