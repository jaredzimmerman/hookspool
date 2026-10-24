"""Google Drive push notifications (https://developers.google.com/drive/api/guides/push).

A watch channel (``changes.watch``) makes Google POST an EMPTY body with ``X-Goog-*`` headers each time the
change feed of a user (or of one shared drive) has something new; the channel was created with a ``token``
that Google echoes in ``X-Goog-Channel-Token``. That echo is the only authentication, compared in constant
time. The channel rules (the token check, the ``sync`` ping, the payload built from the headers, the dedup id)
are ``hookspool.providers.google``'s, shared with the Calendar bobbin; this module only says what the key is.

``payload`` is built from the headers (there is no body)::

    {"event_type": "change", "channel_id": ..., "resource_id": ..., "resource_state": "change",
     "resource_uri": ..., "message_number": "7", "source": "google-drive"}

``resource_state == "sync"`` is the ping Google sends when a channel is created: it is verified, acknowledged
and not enqueued (the processor stores its first page token on the first real push instead).

``key`` is the watched resource (``X-Goog-Resource-ID``; the channel id when Google sent none): a push says
only that the feed moved, so every push of one watched feed is one batch and one pass of the processor
(``calendar`` span, 45 s quiet / 180 s max, durable). ``dedup = <channel>:<message number>``: the number counts up per channel, and
Google resends the same number when it retries a push.

Google retries a failed push with backoff for a limited time (``sender_retries``). A channel expires (at most
a week for the change feed) unless it is renewed: set ``expect_every_s`` in the instance so silence opens an
issue, and keep the renewal sweep (SETUP.md).

Secret: ``channel_token``.
"""
from __future__ import annotations

from hookspool.plugin import Request, Secrets
from hookspool.providers import google as g

name = "drive"
secrets = ("channel_token",)
auth_headers = g.CHANNEL_AUTH_HEADERS
keep_headers = ()
span = "calendar"
expect_every_s = None
sender_retries = True

SOURCE = "google-drive"


def verify(req: Request, secrets: Secrets) -> bool:
    return g.verify_channel(req, secrets["channel_token"])


def ignore(req: Request) -> str | None:
    return g.ignore_handshake(req)


def payload(req: Request) -> dict:
    return g.channel_payload(req, SOURCE)


def key(payload: dict, req: Request) -> str:
    return payload.get("resource_id") or payload.get("channel_id") or "changes"


def dedup_id(payload: dict, req: Request) -> str | None:
    return g.channel_dedup_id(payload)


def event_type(payload: dict, req: Request) -> str:
    return payload["resource_state"]


def attrs(payload: dict, req: Request) -> dict:
    return {"state": payload["resource_state"]}
