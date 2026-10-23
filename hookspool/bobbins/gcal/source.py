"""Google Calendar push notifications (https://developers.google.com/calendar/api/guides/push).

A watch channel (``events.watch``) makes Google POST an EMPTY body with ``X-Goog-*`` headers each
time the calendar changes; the channel was created with a ``token`` that Google echoes in
``X-Goog-Channel-Token``. That echo is the only authentication, compared in constant time. The channel
rules (the token check, the ``sync`` ping, the payload built from the headers, the dedup id) are
``hookspool.providers.google``'s, shared with the Drive bobbin; this module only says which part of
the resource URI is the calendar.

``payload`` is built from the headers (there is no body)::

    {"event_type": "exists", "channel_id": ..., "resource_id": ..., "resource_state": "exists",
     "resource_uri": ..., "message_number": "7", "source": "google-calendar"}

``resource_state == "sync"`` is the ping Google sends when a channel is created: it is verified,
acknowledged and not enqueued (the processor bootstraps its sync token on the first real change
instead).

``key`` is the calendar id from ``resource_uri`` (``primary`` when it cannot be read): the same
calendar has several live channels during a renewal, and each change arrives once per channel, so
the key is per calendar, not per channel; one batch, one pass of the processor (``calendar`` span,
45 s quiet / 180 s max, durable). ``dedup = <channel>:<message number>``: the number counts up per
channel, and Google resends the same number when it retries a push.

Google retries a failed push with backoff for a limited time (``sender_retries``). A channel
expires (at most a week) unless it is renewed: set ``expect_every_s`` in the instance so silence
opens an issue, and keep the renewal sweep (README.md).

Secret: ``channel_token``.
"""
from __future__ import annotations

from hookspool.plugin import Request, Secrets
from hookspool.providers import google as g

name = "gcal"
secrets = ("channel_token",)
auth_headers = g.CHANNEL_AUTH_HEADERS
keep_headers = ()
span = "calendar"
expect_every_s = None
sender_retries = True

SOURCE = "google-calendar"


def verify(req: Request, secrets: Secrets) -> bool:
    return g.verify_channel(req, secrets["channel_token"])


def ignore(req: Request) -> str | None:
    return g.ignore_handshake(req)


def payload(req: Request) -> dict:
    return g.channel_payload(req, SOURCE)


def calendar_of(payload: dict) -> str:
    return g.resource_segment(payload.get("resource_uri"), "calendars") or "primary"


def key(payload: dict, req: Request) -> str:
    return calendar_of(payload)


def dedup_id(payload: dict, req: Request) -> str | None:
    return g.channel_dedup_id(payload)


def event_type(payload: dict, req: Request) -> str:
    return payload["resource_state"]


def attrs(payload: dict, req: Request) -> dict:
    return {"calendar": calendar_of(payload), "state": payload["resource_state"]}
