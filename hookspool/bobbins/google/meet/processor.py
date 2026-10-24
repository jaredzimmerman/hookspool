"""Google Meet processor: one batch of a call's events -> ``meeting-started`` or ``meeting-ended``.

A batch holds the events of one conference record (the source's key), coalesced on the ``meeting`` span, so a short
call (start, joins, leaves, end inside 15 minutes) is one batch. No model is involved and nothing but identity and
times is read: no transcripts, no recordings, no chat. A pass:

1. A batch with ``conference.v2.ended``: reads the conference record and its participants (and each participant's
   sessions) from Meet REST v2 through ``hookspool.providers.google`` on ``ctx.oauth``, and returns one
   ``meeting-ended`` item: the conference id, the space, start, end, and each participant's display name, kind
   (``signedin``, ``anonymous`` or ``phone``), user id, first join and last leave.
2. A batch without it: one ``meeting-started`` item from the events alone (no API call), the first time a batch of
   that conference is seen. A later batch of the same call (a call longer than the span's 15 minutes) or a stray event
   after the end says nothing.
3. A subscription lifecycle event (expiration reminder, expired, suspended): one ``subscription-changed`` item, so a
   deployment can renew or recreate the subscription.

State: ``meet_state`` ``{"seen": {conference record: {"state": "started" | "ended", "at": time}}}``, pruned after two
days. The runtime stages the write and applies it after the journal holds the result, so a retried batch reports the
same items once.
"""
from __future__ import annotations

from datetime import datetime, timezone

from hookspool.providers import google as G

OUTPUTS = ("meeting-started", "meeting-ended", "subscription-changed")       # the manifest's [outputs]
STATE = "meet_state"
SOURCE = "google-meet"
KEEP_S = 2 * 86400

MEET = "google.workspace.meet."
STARTED = MEET + "conference.v2.started"
ENDED = MEET + "conference.v2.ended"
JOINED = MEET + "participant.v2.joined"
LIFECYCLE = {"google.workspace.events.subscription.v1.expirationReminder": "expiration-reminder",
             "google.workspace.events.subscription.v1.expired": "expired",
             "google.workspace.events.subscription.v1.suspended": "suspended"}
SPACE_PREFIX = "//meet.googleapis.com/"


def conference_id(record: str) -> str:
    return record.rpartition("/")[2]


def space_of(events: list[dict]) -> str:
    """``spaces/<id>`` when an event's CloudEvent subject names the meeting space, else ""."""
    for e in events:
        subject = str(e.get("subject") or "")
        if subject.startswith(SPACE_PREFIX + "spaces/"):
            return subject[len(SPACE_PREFIX):]
    return ""


def first_time(events: list[dict], event_type: str | None = None) -> str:
    times = sorted(str(e["event_time"]) for e in events
                   if e.get("event_time") and (event_type is None or e.get("event_type") == event_type))
    return times[0] if times else ""


def last_time(events: list[dict], event_type: str) -> str:
    times = sorted(str(e["event_time"]) for e in events if e.get("event_time") and e.get("event_type") == event_type)
    return times[-1] if times else ""


def base_item(kind: str, record: str) -> dict:
    return {"event_type": kind, "source": SOURCE, "key": "meet:" + conference_id(record),
            "conference_id": conference_id(record), "conference_record": record}


def started_item(record: str, events: list[dict]) -> dict:
    joined = {e.get("participant_session") for e in events if e.get("event_type") == JOINED and e.get("participant_session")}
    return {**base_item("meeting-started", record), "space": space_of(events),
            "start": first_time(events, STARTED) or first_time(events), "joined": len(joined)}


def ended_item(ctx, record: str, events: list[dict], api: str) -> dict:
    client = ctx.oauth("cred")
    rec = G.meet_conference(client, record, base=api)
    participants: list[dict] = []
    complete = True
    if rec is None:
        ctx.log("conference record not found; reporting the events only")
        rec = {}
    else:
        participants, stats = G.meet_attendance(client, record, base=api)
        complete = not stats["truncated"]
        ctx.log(f"ended: participants={stats['participants']} session_lookups={stats['lookups']}"
                + ("; participant listing cut short" if stats["truncated"] else ""))
    return {**base_item("meeting-ended", record), "space": str(rec.get("space") or "") or space_of(events),
            "start": str(rec.get("startTime") or "") or first_time(events, STARTED) or first_time(events),
            "end": str(rec.get("endTime") or "") or last_time(events, ENDED),
            "participant_count": len(participants), "participants_complete": complete,
            "participants": participants}


def lifecycle_items(events: list[dict]) -> list[dict]:
    last: dict[tuple[str, str], dict] = {}
    for e in events:
        kind = LIFECYCLE.get(str(e.get("event_type")))
        name = e.get("subscription_name")
        if kind and name:
            last.pop((name, kind), None)
            last[(name, kind)] = e
    out = []
    for (name, kind), e in last.items():
        out.append({"event_type": "subscription-changed", "source": SOURCE, "key": "subscription:" + name.rpartition("/")[2],
                    "lifecycle": kind, "subscription": name, "expire_time": e.get("expire_time") or "",
                    "state": e.get("state") or "", "suspension_reason": e.get("suspension_reason") or "",
                    "event_time": e.get("event_time") or ""})
    return out


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _age_s(value: object, now: float) -> float:
    try:
        return now - datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return float("inf")


def process(batch, ctx) -> list[dict]:
    events = [e for e in batch.events if isinstance(e, dict)]
    out = lifecycle_items(events)
    by_record: dict[str, list[dict]] = {}
    for e in events:
        if e.get("conference_record"):
            by_record.setdefault(str(e["conference_record"]), []).append(e)
    if not by_record:
        ctx.log(f"events={len(events)} -> {len(out)} lifecycle item(s)")
        return out

    now = ctx.now()
    state = ctx.load_state(STATE, {})
    seen = {k: v for k, v in (state.get("seen") or {}).items()
            if isinstance(v, dict) and _age_s(v.get("at"), now) < KEEP_S}
    api = str(ctx.settings["api_url"])
    for record, evs in by_record.items():
        prior = (seen.get(record) or {}).get("state")
        if any(e.get("event_type") == ENDED for e in evs):
            if prior == "ended":
                ctx.log("ended again: already reported")
                continue
            out.append(ended_item(ctx, record, evs, api))
            seen[record] = {"state": "ended", "at": _iso(now)}
        elif prior is None:
            out.append(started_item(record, evs))
            seen[record] = {"state": "started", "at": _iso(now)}
        else:
            ctx.log(f"events={len(evs)} of a call already reported as {prior}: nothing to say")
    ctx.save_state(STATE, {"seen": seen})
    ctx.log(f"events={len(events)} conferences={len(by_record)} -> {len(out)} item(s)")
    return out
