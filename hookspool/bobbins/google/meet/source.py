"""Google Meet events through the Google Workspace Events API and Cloud Pub/Sub
(https://developers.google.com/workspace/events/guides/events-meet).

A Workspace Events subscription on a user (``//cloudidentity.googleapis.com/users/<numeric id>``) publishes each
Meet event to a Pub/Sub topic; a push subscription POSTs it here as the usual Pub/Sub envelope. The event is a
CloudEvent carried in the message ``attributes`` (``ce-type``, ``ce-id``, ``ce-time``, ``ce-subject``,
``ce-source``), and ``data`` is base64 JSON naming the resource and nothing else::

    {"message": {"attributes": {"ce-type": "google.workspace.meet.conference.v2.started", "ce-id": "...", ...},
                 "data": "<base64 of {\"conferenceRecord\": {\"name\": \"conferenceRecords/<id>\"}}>",
                 "messageId": "123", "publishTime": "..."},
     "subscription": "projects/<project>/subscriptions/<subscription>"}

Participant events carry ``{"participantSession": {"name": "conferenceRecords/<id>/participants/<id>/
participantSessions/<id>"}}`` instead. The subscription's lifecycle events
(``google.workspace.events.subscription.v1.expirationReminder``, ``.expired``, ``.suspended``) reach the same
topic with ``{"subscription": {"name": "subscriptions/<id>", ...}}``.

Verification is the push token, as for google.gmail: the push endpoint URL carries ``?token=...`` (or the
``X-Push-Token`` header), compared in constant time by ``hookspool.providers.google.verify_push``; the envelope
is unwrapped by ``push_message``. The query string is never logged or stored.

``payload`` (a push that does not decode raises ``BadPayload`` and is quarantined)::

    {"event_type": <ce-type>, "event_id": <ce-id>, "event_time": <ce-time>, "subject": <ce-subject>,
     "conference_record": "conferenceRecords/<id>", "participant_session": "<name>" (participant events),
     "messageId", "publishTime", "subscription"}

    a lifecycle event instead of the two resource names:
    {"subscription_name": "subscriptions/<id>", "expire_time": ..., "state": ..., "suspension_reason": ...}

``key`` is the conference record (a lifecycle event: the subscription), so the start, joins, leaves and end of one
call coalesce into one batch on the ``meeting`` span (60 s quiet, 900 s max). ``dedup = ce-id`` (else the Pub/Sub
``messageId``): Workspace Events keeps the CloudEvent id when Pub/Sub delivers a message again. ``event_type`` is
the ce-type. Other Meet event types (recordings, transcripts, smart notes) and any type added later are verified,
acknowledged and not queued. Pub/Sub retries a non-2xx for the subscription's retention (``sender_retries``).

Secret: ``push_token``.
"""
from __future__ import annotations

import re

from hookspool.plugin import BadPayload, Request, Secrets
from hookspool.providers import google as G

name = "meet"
secrets = ("push_token",)
auth_headers = G.PUSH_AUTH_HEADERS
keep_headers = ()
span = "meeting"
expect_every_s = None
sender_retries = True

MEET = "google.workspace.meet."
LIFECYCLE = "google.workspace.events.subscription.v1."
CONFERENCE_EVENTS = (MEET + "conference.v2.started", MEET + "conference.v2.ended")
PARTICIPANT_EVENTS = (MEET + "participant.v2.joined", MEET + "participant.v2.left")
LIFECYCLE_EVENTS = (LIFECYCLE + "expirationReminder", LIFECYCLE + "expired", LIFECYCLE + "suspended")
HANDLED = CONFERENCE_EVENTS + PARTICIPANT_EVENTS + LIFECYCLE_EVENTS

_ID = r"[A-Za-z0-9_-]{1,128}"
SESSION_RE = re.compile(rf"^(conferenceRecords/{_ID})/participants/{_ID}/participantSessions/{_ID}$")
SUBSCRIPTION_RE = re.compile(rf"^subscriptions/{_ID}$")
_TEXT_MAX = 256


def verify(req: Request, secrets: Secrets) -> bool:
    return G.verify_push(req, secrets["push_token"])


def ignore(req: Request) -> str | None:
    """A verified push of an event type this bobbin does not handle: acknowledged, not queued. A push that does not
    decode is left to ``payload``, which refuses it."""
    try:
        et = G.push_message(req)["attributes"].get("ce-type")
    except BadPayload:
        return None
    return "unhandled event type" if et and et not in HANDLED else None


def _text(value: object) -> str:
    return value[:_TEXT_MAX] if isinstance(value, str) else ""


def _name(obj: object) -> str:
    return obj.get("name") if isinstance(obj, dict) and isinstance(obj.get("name"), str) else ""


def payload(req: Request) -> dict:
    msg = G.push_message(req)
    a, data = msg["attributes"], msg["data"]
    et = a.get("ce-type") or ""
    if et not in HANDLED:
        raise BadPayload("push has no Meet event type")
    out: dict = {"event_type": et, "event_id": _text(a.get("ce-id")), "event_time": _text(a.get("ce-time")),
                 "subject": _text(a.get("ce-subject"))}
    if et in LIFECYCLE_EVENTS:
        sub = data.get("subscription") if isinstance(data.get("subscription"), dict) else {}
        if not SUBSCRIPTION_RE.match(_name(sub)):
            raise BadPayload("lifecycle event has no subscription name")
        out["subscription_name"] = sub["name"]
        out["expire_time"] = _text(sub.get("expireTime") or sub.get("expire_time"))
        out["state"] = _text(sub.get("state"))
        out["suspension_reason"] = _text(sub.get("suspensionReason") or sub.get("suspension_reason"))
    elif et in CONFERENCE_EVENTS:
        record = _name(data.get("conferenceRecord"))
        if not G.MEET_RECORD_RE.match(record):
            raise BadPayload("conference event has no conference record name")
        out["conference_record"] = record
    else:
        session = _name(data.get("participantSession"))
        m = SESSION_RE.match(session)
        if not m:
            raise BadPayload("participant event has no participant session name")
        out["conference_record"], out["participant_session"] = m.group(1), session
    for f in ("messageId", "publishTime", "subscription"):
        if f in msg:
            out[f] = msg[f]
    return out


def key(payload: dict, req: Request) -> str:
    return payload.get("conference_record") or payload["subscription_name"]


def dedup_id(payload: dict, req: Request) -> str | None:
    return payload.get("event_id") or payload.get("messageId")


def event_type(payload: dict, req: Request) -> str:
    return payload["event_type"]


def attrs(payload: dict, req: Request) -> dict:
    return {"event_type": payload["event_type"]}
