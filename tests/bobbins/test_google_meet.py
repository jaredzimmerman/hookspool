"""The ``google.meet`` bobbin (ARCHITECTURE 14.3): its source (push token, CloudEvent decoding, key, dedup, the event
types it ignores) through the shared source kit and its captures, and its processor through the processor runtime
(journal, staged state, emits) against a scripted Google (``tests/_googlestub.py``):

* a call: ``meeting-started`` once, ``meeting-ended`` with the attendance read from Meet REST v2, a short call as one
  batch, later batches of a long call and stray events after the end saying nothing;
* participants: kinds, user ids, first join and last leave, sessions, paging, ordering;
* lifecycle events of the subscription as ``subscription-changed``;
* failures: ``Retryable`` (exit 75) / ``Permanent`` (exit 65), a missing conference record, and that a failed pass
  changes no state;
* the 14.3.7 rules and the documents of 14.3.8.
"""
from __future__ import annotations

import base64
import json
import os

import pytest

import _bobbinrig
import _bobbinrules as R
import _googlestub
import sourcekit
from sourcekit import SENTINEL, Env, Kit, request

from hookspool import bobbin_loader as PK
from hookspool import config as C
from hookspool import plugin as P
from hookspool.bobbins.google.meet import processor as mp
from hookspool.bobbins.google.meet import source as meet
from hookspool.plugin import BadPayload, Request, parse_capture

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DIR = os.path.join(ROOT, "hookspool", "bobbins", "google", "meet")
TOKEN = "test-meet-push-token-" + SENTINEL
SUB = "projects/test-project/subscriptions/" + SENTINEL
REC = "conferenceRecords/conf-1"
M = "google.workspace.meet."
STARTED, ENDED = M + "conference.v2.started", M + "conference.v2.ended"
JOINED, LEFT = M + "participant.v2.joined", M + "participant.v2.left"
LC = "google.workspace.events.subscription.v1."
T0 = 1_800_000_000.0


# ---- the source -------------------------------------------------------------------------------------------
def session(rec=REC, part="p-1", sess="s-1") -> str:
    return f"{rec}/participants/{part}/participantSessions/{sess}"


def data_for(ce_type: str, rec: str = REC, **kw) -> dict:
    if ce_type.startswith(LC):
        return {"subscription": {"name": kw.get("sub_name", "subscriptions/sub-1"), "expireTime": "2026-10-30T10:00:00Z"}}
    if ce_type in (STARTED, ENDED):
        return {"conferenceRecord": {"name": rec}}
    if ce_type in (JOINED, LEFT):
        return {"participantSession": {"name": kw.get("sess") or session(rec)}}
    return {"recording": {"name": rec + "/recordings/r-1"}}


def envelope(ce_type=STARTED, ce_id="ev-1", rec=REC, *, data=None, attrs=None, message_id="m-1", pad=True, **kw) -> bytes:
    a = {"ce-type": ce_type, "ce-id": ce_id, "ce-time": "2026-10-23T10:00:00Z",
         "ce-subject": "//meet.googleapis.com/spaces/space-1",
         "ce-source": "//workspaceevents.googleapis.com/subscriptions/sub-1", "ce-specversion": "1.0"}
    a = {k: v for k, v in {**a, **(attrs or {})}.items() if v is not None}
    raw = base64.b64encode(json.dumps(data_for(ce_type, rec, **kw) if data is None else data).encode()).decode()
    msg = {"attributes": a, "data": raw if pad else raw.rstrip("="), "publishTime": "2026-10-23T10:00:01Z"}
    if message_id:
        msg["messageId"] = message_id
    return json.dumps({"message": msg, "subscription": SUB}).encode()


def sign(raw, secrets, now):
    """The push token in the endpoint URL, as the push subscription sends it."""
    sep = "&" if "?" in raw.target else "?"
    return P.RawRequest(raw.method, f"{raw.target}{sep}token={secrets['push_token']}", list(raw.headers), raw.body,
                        dict(raw.meta))


def _target(token):
    return "/hooks/meet" + (f"?token={token}" if token is not None else "")


KIT = Kit(name="meet", module=meet, secrets={"push_token": TOKEN}, sign=sign, body_signed=False,
          source_cfg={"action": "script:meet"},
          sample=request("POST", "/hooks/meet", {"Content-Type": "application/json"}, envelope()))


@pytest.mark.parametrize("case", sourcekit.CASES, ids=sourcekit.CASE_IDS)
def test_kit_matrix(case, tmp_path, clock):
    sourcekit.run(case, KIT, tmp_path, clock)


CAPTURES = sourcekit.captures("google.meet")


@pytest.mark.parametrize("path", CAPTURES, ids=[os.path.basename(p) for p in CAPTURES])
def test_captures(path, tmp_path, clock):
    assert sourcekit.run_capture(path, KIT, tmp_path, clock)["source"] == "meet"


def test_captures_cover_the_outcomes():
    got = {}
    for c in CAPTURES:
        with open(c, "rb") as f:
            got[os.path.basename(c)] = parse_capture(f.read()).meta["expect"]
    assert got == {"conference_started.http": "queued", "participant_joined.http": "queued",
                   "conference_ended.http": "queued", "other_event.http": "ignored",
                   "wrong_token.http": "unverified", "no_token.http": "unverified"}, got


@pytest.fixture
def env(tmp_path, clock):
    e = Env(KIT, tmp_path, clock)
    yield e
    e.close()


def post(env, body: bytes, token=TOKEN, header=None):
    raw = request("POST", _target(token), {"Content-Type": "application/json"}, body)
    if header is not None:
        raw = raw.set("X-Push-Token", header)
    return env.send(raw)


def status(r):
    return r.status, (json.loads(r.body)["status"] if r.body else "")


def test_wrong_missing_and_empty_tokens_are_401(env):
    for kw in ({"token": "nope"}, {"token": None}, {"token": ""}, {"token": TOKEN[:-1]}, {"token": TOKEN + "x"},
               {"token": None, "header": "nope"}):
        assert (post(env, envelope(), **kw).status, post(env, envelope(), **kw).body) == (401, b""), kw
    assert env.events() == []


def test_the_token_works_in_the_header_too_and_the_query_wins(env):
    assert status(post(env, envelope(ce_id="a"), token=None, header=TOKEN)) == (200, "queued")
    assert post(env, envelope(ce_id="b"), token="nope", header=TOKEN).status == 401


def test_an_unhandled_event_type_is_verified_then_acknowledged_and_not_queued(env):
    rec = envelope(M + "recording.v2.fileGenerated")
    assert post(env, rec, token="nope").status == 401, "verify first"
    assert status(post(env, rec)) == (200, "ignored")
    assert status(post(env, envelope(M + "transcript.v2.started"))) == (200, "ignored")
    assert env.batches() == [] and env.events() == []


def test_the_token_is_not_stored_or_logged(env):
    post(env, envelope())
    env.assert_no_secret_in_db()
    env.assert_clean_logs()
    stored = json.loads(env.events()[0]["headers"] or "{}")
    assert not any(TOKEN in str(v) for v in stored.values()) and "X-Push-Token" not in stored


def classify(body: bytes):
    return P.Source("meet", meet, {}).classify(Request("POST", "/hooks/meet", [], body, now=0))


def test_a_conference_event_is_normalised():
    c = classify(envelope(STARTED, "ev-7"))
    assert c.payload == {"event_type": STARTED, "event_id": "ev-7", "event_time": "2026-10-23T10:00:00Z",
                         "subject": "//meet.googleapis.com/spaces/space-1", "conference_record": REC,
                         "messageId": "m-1", "publishTime": "2026-10-23T10:00:01Z", "subscription": SUB}
    assert c.key == REC and c.dedup_id == "ev-7" and c.event_type == STARTED
    assert c.attrs == {"event_type": STARTED} and c.default_action is None


def test_a_participant_event_is_keyed_by_its_conference_record():
    c = classify(envelope(JOINED, sess=session("conferenceRecords/conf-9", "p-2", "s-3")))
    assert c.key == "conferenceRecords/conf-9"
    assert c.payload["participant_session"] == "conferenceRecords/conf-9/participants/p-2/participantSessions/s-3"


def test_a_lifecycle_event_is_keyed_by_the_subscription():
    c = classify(envelope(LC + "expirationReminder", data={"subscription": {
        "name": "subscriptions/sub-1", "expire_time": "2026-10-30T10:00:00Z", "state": "ACTIVE"}}))
    assert c.key == "subscriptions/sub-1" and c.event_type == LC + "expirationReminder"
    assert c.payload["expire_time"] == "2026-10-30T10:00:00Z" and c.payload["state"] == "ACTIVE"
    c = classify(envelope(LC + "suspended", data={"subscription": {"name": "subscriptions/sub-1",
                                                                     "suspensionReason": "ENDPOINT_PERMISSION_DENIED"}}))
    assert c.payload["suspension_reason"] == "ENDPOINT_PERMISSION_DENIED"


def test_dedup_is_the_cloudevent_id_else_the_message_id():
    assert classify(envelope(ce_id="ev-1", message_id="m-9")).dedup_id == "ev-1"
    assert classify(envelope(attrs={"ce-id": None}, message_id="m-9")).dedup_id == "m-9"
    assert classify(envelope(attrs={"ce-id": None}, message_id=None)).dedup_id is None


def test_unpadded_base64_decodes():
    assert classify(envelope(pad=False)).key == REC


def b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode() if not isinstance(obj, bytes) else obj).decode()


def env_with(data, ce_type=STARTED) -> bytes:
    return json.dumps({"message": {"attributes": {"ce-type": ce_type, "ce-id": "x"}, "data": data}}).encode()


@pytest.mark.parametrize("body", [
    b"[]", b"{}", b'{"message": 5}', b'{"message": {}}', b"not json",
    env_with(""), env_with("!!!not base64!!!"), env_with(b64(b"not json")), env_with(b64([1, 2])),
    json.dumps({"message": {"data": b64({"conferenceRecord": {"name": REC}})}}).encode(),          # no ce-type
    env_with(b64({})), env_with(b64({"conferenceRecord": {"name": "spaces/s-1"}})),
    env_with(b64({"conferenceRecord": {"name": "conferenceRecords/../../v1/x"}})),
    env_with(b64({"conferenceRecord": {"name": REC + "/participants"}})),
    env_with(b64({"conferenceRecord": "conf-1"})),
    env_with(b64({"participantSession": {"name": REC + "/participants/p-1"}}), JOINED),
    env_with(b64({"participantSession": {"name": REC + "/participants/p-1/participantSessions/s/x"}}), LEFT),
    env_with(b64({"conferenceRecord": {"name": REC}}), JOINED),
    env_with(b64({"subscription": {"name": "subs/sub-1"}}), LC + "expired"),
    env_with(b64({"subscription": "subscriptions/sub-1"}), LC + "expired"),
], ids=lambda b: b[:40].decode("latin-1"))
def test_an_undecodable_push_is_a_bad_payload(body):
    with pytest.raises(BadPayload):
        classify(body)


def test_an_undecodable_push_is_quarantined_not_dropped(env):
    r = post(env, env_with(b64({"conferenceRecord": {"name": "nope"}})))
    assert (r.status, json.loads(r.body)["status"]) == (202, "quarantined")
    b = env.batches()
    assert len(b) == 1 and b[0]["destination"] == "quarantine" and b[0]["state"] == "dead"


def test_one_calls_events_coalesce_on_the_meeting_span_and_other_calls_do_not(env):
    assert status(post(env, envelope(STARTED, "e1"))) == (200, "queued")
    assert status(post(env, envelope(JOINED, "e2"))) == (200, "queued")
    assert status(post(env, envelope(ENDED, "e3"))) == (200, "queued")
    assert status(post(env, envelope(STARTED, "e4", "conferenceRecords/conf-2"))) == (200, "queued")
    rows = {r["key"]: r for r in env.batches()}
    assert set(rows) == {REC, "conferenceRecords/conf-2"} and rows[REC]["count"] == 3
    assert rows[REC]["destination"] == "script:meet"
    assert (rows[REC]["quiet_s"], rows[REC]["max_s"]) == (60.0, 900.0)


def test_a_redelivered_event_is_a_duplicate(env):
    assert status(post(env, envelope(ce_id="same", message_id="m-1"))) == (200, "queued")
    assert status(post(env, envelope(ce_id="same", message_id="m-2"))) == (200, "duplicate")


def test_declarations():
    assert (meet.span, meet.sender_retries, meet.secrets) == ("meeting", True, ("push_token",))
    assert not hasattr(meet, "action") and meet.auth_headers == ("X-Push-Token",) and meet.keep_headers == ()
    assert C.DEFAULTS["spans"]["meeting"] == {"quiet_s": 60, "max_s": 900}


# ---- the processor: fixtures ------------------------------------------------------------------------------
@pytest.fixture
def google():
    g = _googlestub.GoogleStub()
    yield g
    g.close()


@pytest.fixture
def make(tmp_path, google):
    def build_rig(**over):
        cred = _bobbinrig.write_cred(tmp_path / "creds" / "meet.json", google.port)
        return _bobbinrig.Rig(tmp_path, "google.meet", mp.OUTPUTS, {**google.meet_settings(), **over}, {"cred": cred})
    return build_rig


def ev(ce_type, t="2026-10-23T10:00:00Z", rec=REC, part="p-1", n=1) -> dict:
    """A stored payload, as the source made it."""
    e = {"event_type": ce_type, "event_id": f"ev-{n}", "event_time": t, "subject": "//meet.googleapis.com/spaces/space-1",
         "conference_record": rec, "messageId": f"m-{n}"}
    if ce_type in (JOINED, LEFT):
        e["participant_session"] = f"{rec}/participants/{part}/participantSessions/s-{n}"
    return e


def person(pid, kind="signedin", name="Someone", user="users/1001", first="2026-10-23T10:00:05Z",
           last="2026-10-23T10:03:50Z", rec=REC) -> dict:
    p = {"name": f"{rec}/participants/{pid}"}
    if first:
        p["earliestStartTime"] = first
    if last:
        p["latestEndTime"] = last
    if kind == "signedin":
        p["signedinUser"] = {"user": user, "displayName": name}
    elif kind == "anonymous":
        p["anonymousUser"] = {"displayName": name}
    elif kind == "phone":
        p["phoneUser"] = {"displayName": name}
    return p


def a_call(google, *people, rec=REC):
    google.meet_records[rec] = {"name": rec, "startTime": "2026-10-23T10:00:00Z", "endTime": "2026-10-23T10:04:00Z",
                                "space": "spaces/space-1", "expireTime": "2026-11-22T10:04:00Z"}
    google.meet_participants[rec] = [{"participants": list(people)}]


def kinds(items):
    return [i["event_type"] for i in items]


# ---- a call -----------------------------------------------------------------------------------------------
def test_a_short_call_is_one_batch_and_one_meeting_ended(make, google):
    rig = make()
    a_call(google, person("p-1", name="Alex"), person("p-2", "anonymous", "Guest", first="2026-10-23T10:01:00Z"))
    google.meet_sessions[REC + "/participants/p-1"] = [{"name": "s1", "startTime": "a", "endTime": "b"},
                                                       {"name": "s2", "startTime": "c", "endTime": "d"}]
    (item,) = rig.items(ev(STARTED), ev(JOINED, n=2), ev(LEFT, n=3), ev(ENDED, "2026-10-23T10:04:01Z", n=4))
    assert item == {
        "event_type": "meeting-ended", "source": "google-meet", "key": "meet:conf-1", "conference_id": "conf-1",
        "conference_record": REC, "space": "spaces/space-1", "start": "2026-10-23T10:00:00Z",
        "end": "2026-10-23T10:04:00Z", "participant_count": 2, "participants_complete": True,
        "participants": [
            {"display_name": "Alex", "kind": "signedin", "user_id": "1001", "first_join": "2026-10-23T10:00:05Z",
             "last_leave": "2026-10-23T10:03:50Z", "sessions": 2},
            {"display_name": "Guest", "kind": "anonymous", "user_id": "", "first_join": "2026-10-23T10:01:00Z",
             "last_leave": "2026-10-23T10:03:50Z", "sessions": 0}]}
    paths = [r["path"] for r in google.requests if r["path"].startswith("/meet/")]
    assert paths[0] == "/meet/" + REC and paths[1] == "/meet/" + REC + "/participants"
    assert not any("transcript" in p or "recording" in p for p in paths), "no transcripts, no recordings"
    assert rig.get_state(mp.STATE)["seen"][REC]["state"] == "ended"


def test_a_long_call_says_started_once_then_ended(make, google):
    rig = make()
    assert kinds(rig.items(ev(STARTED), ev(JOINED, n=2), bid="1")) == ["meeting-started"]
    assert google.count("GET", "/meet/") == 0, "a start needs no API call"
    assert rig.items(ev(JOINED, n=3), ev(LEFT, n=4), bid="2") == [], "a later batch of the same call says nothing"
    a_call(google, person("p-1"))
    assert kinds(rig.items(ev(LEFT, n=5), ev(ENDED, n=6), bid="3")) == ["meeting-ended"]
    assert rig.items(ev(LEFT, n=7), bid="4") == [], "a stray event after the end says nothing"
    assert rig.items(ev(ENDED, n=8), bid="5") == [], "a second end is not reported again"


def test_meeting_started_carries_the_events_facts(make, google):
    rig = make()
    (item,) = rig.items(ev(JOINED, "2026-10-23T10:00:09Z", n=2), ev(STARTED, "2026-10-23T10:00:00Z"),
                        ev(JOINED, "2026-10-23T10:00:20Z", part="p-2", n=3))
    assert item == {"event_type": "meeting-started", "source": "google-meet", "key": "meet:conf-1",
                    "conference_id": "conf-1", "conference_record": REC, "space": "spaces/space-1",
                    "start": "2026-10-23T10:00:00Z", "joined": 2}


def test_a_batch_without_a_start_event_still_starts_from_the_earliest_event(make, google):
    rig = make()
    (item,) = rig.items(ev(JOINED, "2026-10-23T10:05:00Z", n=2), ev(JOINED, "2026-10-23T10:02:00Z", n=3))
    assert item["event_type"] == "meeting-started" and item["start"] == "2026-10-23T10:02:00Z"


def test_participant_kinds_ids_and_times(make, google):
    rig = make()
    a_call(google, person("p-3", "phone", "+1 ***-***-0100", first="2026-10-23T10:02:00Z", last=""),
           person("p-1", user="users/2002", name="Sam", first="2026-10-23T10:00:01Z"),
           {"name": REC + "/participants/p-9"})
    google.meet_sessions[REC + "/participants/p-3"] = [{"startTime": "2026-10-23T10:02:00Z",
                                                        "endTime": "2026-10-23T10:03:00Z"}]
    out = rig.items(ev(ENDED))[0]["participants"]
    assert [(p["kind"], p["user_id"], p["display_name"]) for p in out] == [
        ("signedin", "2002", "Sam"), ("phone", "", "+1 ***-***-0100"), ("unknown", "", "")]
    assert out[1]["last_leave"] == "2026-10-23T10:03:00Z", "a missing latestEndTime falls back to the sessions"
    assert out[2]["first_join"] == "" and out[2]["sessions"] == 0


def test_the_participant_listing_follows_pages(make, google):
    rig = make()
    a_call(google)
    google.meet_participants[REC] = [{"participants": [person("p-1")], "nextPageToken": "p1"},
                                     {"participants": [person("p-2")], "nextPageToken": "p2"},
                                     {"participants": [person("p-3")]}]
    item = rig.items(ev(ENDED))[0]
    assert item["participant_count"] == 3 and item["participants_complete"] is True
    pages = [r["query"].get("pageToken") for r in google.requests if r["path"] == "/meet/" + REC + "/participants"]
    assert pages == [None, "p1", "p2"]


def test_a_missing_conference_record_reports_the_events_only(make, google):
    rig = make()
    (item,) = rig.items(ev(STARTED, "2026-10-23T10:00:00Z"), ev(ENDED, "2026-10-23T10:09:00Z", n=2))
    assert item["participants"] == [] and item["participant_count"] == 0
    assert (item["start"], item["end"], item["space"]) == ("2026-10-23T10:00:00Z", "2026-10-23T10:09:00Z",
                                                            "spaces/space-1")
    assert google.count("GET", "/meet/" + REC + "/participants") == 0


def test_a_server_error_is_a_retry_and_leaves_the_state(make, google):
    rig = make()
    rig.items(ev(STARTED), bid="1")
    before = rig.get_state(mp.STATE)
    google.meet_records[REC] = 500
    rc, emits, err = rig.run(ev(ENDED, n=2), bid="2")
    assert rc == 75 and emits == [] and rig.get_state(mp.STATE) == before
    assert "will be retried" in err


def test_a_refused_request_is_permanent(make, google):
    rig = make()
    a_call(google)
    google.meet_participants[REC] = 403
    rc, emits, err = rig.run(ev(ENDED))
    assert rc == 65 and emits == [] and "permanent failure" in err


def test_a_retried_batch_replays_the_journal_and_reads_meet_once(make, google):
    rig = make()
    a_call(google, person("p-1"))
    first = rig.run(ev(ENDED))
    again = rig.run(ev(ENDED))
    assert first[0] == again[0] == 0 and [e["payload"] for e in first[1]] == [e["payload"] for e in again[1]]
    assert [r["path"] for r in google.requests].count("/meet/" + REC + "/participants") == 1


def test_the_seen_list_forgets_a_call_after_two_days(make, google):
    rig = make()
    rig.items(ev(STARTED), bid="1", clock=lambda: T0)
    assert rig.items(ev(JOINED, n=2), bid="2", clock=lambda: T0 + 3600) == []
    rig.items(ev(STARTED, rec="conferenceRecords/conf-2"), bid="3", clock=lambda: T0 + 3 * 86400)
    assert set(rig.get_state(mp.STATE)["seen"]) == {"conferenceRecords/conf-2"}


def test_lifecycle_events_become_subscription_changed(make, google):
    rig = make()
    evs = [{"event_type": LC + "expirationReminder", "event_id": "l1", "event_time": "2026-10-29T22:00:00Z",
            "subscription_name": "subscriptions/sub-1", "expire_time": "2026-10-30T10:00:00Z", "state": "ACTIVE",
            "suspension_reason": ""},
           {"event_type": LC + "expirationReminder", "event_id": "l2", "event_time": "2026-10-30T09:00:00Z",
            "subscription_name": "subscriptions/sub-1", "expire_time": "2026-10-30T10:00:00Z", "state": "ACTIVE",
            "suspension_reason": ""}]
    (item,) = rig.items(*evs)
    assert item == {"event_type": "subscription-changed", "source": "google-meet", "key": "subscription:sub-1",
                    "lifecycle": "expiration-reminder", "subscription": "subscriptions/sub-1",
                    "expire_time": "2026-10-30T10:00:00Z", "state": "ACTIVE", "suspension_reason": "",
                    "event_time": "2026-10-30T09:00:00Z"}
    assert google.count("GET", "/meet/") == 0 and not rig.has_state(mp.STATE)


def test_the_credential_is_never_rewritten(make, google, tmp_path):
    rig = make()
    a_call(google, person("p-1"))
    cred = tmp_path / "creds" / "meet.json"
    before = cred.read_text()
    rig.items(ev(ENDED))
    assert cred.read_text() == before and google.token_calls == 1
    assert all(r["auth"] == "Bearer tok-refreshed" for r in google.requests if r["path"].startswith("/meet"))


def test_emits_follow_the_routes_and_an_unmapped_output_is_dropped(make, google):
    rig = make()
    rc, emits, err = rig.run(ev(STARTED), routes={"meeting-ended": "hermes:ended"})
    assert rc == 0 and emits == [] and "dropped 1 item(s) of unmapped event types: meeting-started x1" in err


def test_logs_carry_counts_never_names(make, google):
    rig = make()
    a_call(google, person("p-1", name="Very Private Name", user="users/987654321"))
    rc, emits, err = rig.run(ev(ENDED))
    assert rc == 0 and "Very Private Name" not in err and "987654321" not in err and "conf-1" not in err


# ---- the bobbin as a unit (14.3.1, 14.3.7, 14.3.8) --------------------------------------------------------
def package():
    pkgs, errs = PK.discover({})
    assert errs == [], errs
    return pkgs["google.meet"]


def test_the_manifest_is_valid_and_the_outputs_are_the_ones_the_code_produces():
    pkg = package()
    assert pkg.errors == [] and pkg.builtin and pkg.processor.get("state") is True
    assert set(mp.OUTPUTS) == set(pkg.outputs)
    assert pkg.secrets["cred"]["processor"] is True and "processor" not in pkg.secrets["push_token"]
    for f in ("README.md", "SETUP.md", "AGENTS.md", "bobbin.toml", "source.py", "processor.py", "__init__.py"):
        assert os.path.isfile(os.path.join(DIR, f)), f


def test_the_bobbin_calls_core_and_never_re_implements_it():
    assert R.violations(DIR) == []


def test_the_handled_event_types_are_the_ones_the_processor_knows():
    assert set(meet.CONFERENCE_EVENTS) == {mp.STARTED, mp.ENDED} and mp.JOINED in meet.PARTICIPANT_EVENTS
    assert set(meet.LIFECYCLE_EVENTS) == set(mp.LIFECYCLE)


def test_the_setup_documents_name_the_renewal_call_and_the_user_target():
    for doc in ("SETUP.md", "AGENTS.md"):
        with open(os.path.join(DIR, doc), encoding="utf-8") as f:
            text = f.read()
        assert "subscriptions/<SUBSCRIPTION_ID>?updateMask=ttl" in text and '"ttl": "0s"' in text, doc
        assert "//cloudidentity.googleapis.com/users/<USER_ID>" in text and "users/me" in text, doc
        assert "meet-api-event-push@" in text, doc
        assert "meetings.space.readonly" in text, doc
