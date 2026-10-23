"""Google Calendar push notifications (https://developers.google.com/calendar/api/guides/push).

A watch channel (``events.watch``) makes Google POST an EMPTY body with ``X-Goog-*`` headers each
time the calendar changes; the channel was created with a ``token`` that Google echoes in
``X-Goog-Channel-Token``. That echo is the only authentication, compared in constant time.

``payload`` is built from the headers (there is no body)::

    {"event_type": "exists", "channel_id": ..., "resource_id": ..., "resource_state": "exists",
     "resource_uri": ..., "message_number": "7", "source": "google-calendar"}

which is the shape ``deploy/scripts/gcal_delta.py`` already reads. ``resource_state == "sync"`` is
the ping Google sends when a channel is created: it is verified, acknowledged and not enqueued
(the script bootstraps its sync token on the first real change instead).

``key`` is the calendar id from ``resource_uri`` (``primary`` when it cannot be read): the same
calendar has several live channels during a renewal, and each change arrives once per channel, so
the key is per calendar, not per channel; one batch, one pass of the delta script. This replaces
the in-memory debounce of the old bridge (``calendar`` span, 45 s quiet / 180 s max, durable).
``dedup = <channel>:<message number>``: the number counts up per channel, and Google resends the
same number when it retries a push.

Google retries a failed push with backoff for a limited time (``sender_retries``). A channel
expires (at most a week) unless it is renewed: set ``expect_every_s`` in the source's config so
silence opens an issue, and keep the renewal sweep (``docs`` and ``deploy/README.md``).

Action: ``script:gcal_delta`` (``deploy/scripts/gcal_delta.py --batch``). Secret: ``channel_token``.
"""
from __future__ import annotations

import re
from urllib.parse import unquote

from hookspool.plugin import Request, Secrets, verify as v

name = "gcal"
secrets = ("channel_token",)
auth_headers = ("X-Goog-Channel-Token",)
keep_headers = ()
span = "calendar"
expect_every_s = None
sender_retries = True
action = "script:gcal_delta"

_CALENDAR = re.compile(r"/calendars/([^/?]+)")


def verify(req: Request, secrets: Secrets) -> bool:
    return v.static_token(req.header("X-Goog-Channel-Token"), secrets["channel_token"])


def ignore(req: Request) -> str | None:
    return "sync" if req.header("X-Goog-Resource-State") == "sync" else None


def payload(req: Request) -> dict:
    state = req.header("X-Goog-Resource-State") or "unknown"
    return {
        "event_type": state,
        "channel_id": req.header("X-Goog-Channel-ID"),
        "resource_id": req.header("X-Goog-Resource-ID"),
        "resource_state": state,
        "resource_uri": req.header("X-Goog-Resource-URI"),
        "message_number": req.header("X-Goog-Message-Number"),
        "source": "google-calendar",
    }


def calendar_of(payload: dict) -> str:
    m = _CALENDAR.search(payload.get("resource_uri") or "")
    return unquote(m.group(1)) if m else "primary"


def key(payload: dict, req: Request) -> str:
    return calendar_of(payload)


def dedup_id(payload: dict, req: Request) -> str | None:
    ch, n = payload.get("channel_id"), payload.get("message_number")
    return f"{ch}:{n}" if ch and n else None


def event_type(payload: dict, req: Request) -> str:
    return payload["resource_state"]


def attrs(payload: dict, req: Request) -> dict:
    return {"calendar": calendar_of(payload), "state": payload["resource_state"]}
