"""Gmail push notifications via Google Cloud Pub/Sub (https://developers.google.com/gmail/api/guides/push).

A Gmail ``users.watch`` publishes ``{"emailAddress": ..., "historyId": ...}`` to a Pub/Sub topic; a
push subscription POSTs each message to this endpoint as the usual Pub/Sub envelope::

    {"message": {"data": "<base64 JSON>", "messageId": "123", "publishTime": "..."},
     "subscription": "projects/p/subscriptions/s"}

Verification is the push token: Pub/Sub cannot sign a push, so the subscription's endpoint URL
carries a long random ``?token=...`` that only our subscription knows (an ``X-Push-Token`` header
is accepted too, for a proxy that moves it). It is compared in constant time; the check and the envelope
decoding are ``hookspool.providers.google``'s (``verify_push``, ``push_message``), shared with ``google.meet``. The
query string is never logged or stored. Hardening for later: verify the OIDC ``Authorization`` token Pub/Sub can
attach (ARCHITECTURE section 10).

``payload`` is the decoded notification, normalised to ``{emailAddress, historyId, messageId,
publishTime, subscription}`` (``emailAddress`` lower-cased). A push whose envelope does not
decode to that is not silently dropped: ``payload`` raises ``BadPayload`` and the intake
quarantines the request for an operator.

``key = emailAddress``, so one mailbox's pushes coalesce into one batch and the processor
reads the mailbox once for all of them (the ``mail`` span, 30 s quiet / 120 s max).
``dedup = messageId``. A Pub/Sub ``messageId`` is per push, not per mailbox change: if the intake
is killed after COMMIT and before answering, Pub/Sub redelivers a push with a NEW ``messageId``,
which joins the batch as a second event and is reconciled by the processor (it reads Gmail's history
from its stored ``historyId``, so extra pushes cost nothing and lose nothing). Pub/Sub retries a
non-2xx for the subscription's retention (``sender_retries``).

The bobbin's processor (``processor.py``) is the action; the instance's ``outputs`` decide where its
results go. Secret: ``push_token``.
"""
from __future__ import annotations

from hookspool.plugin import BadPayload, Request, Secrets
from hookspool.providers import google as G

name = "gmail"
secrets = ("push_token",)
auth_headers = G.PUSH_AUTH_HEADERS
keep_headers = ()
span = "mail"
expect_every_s = None
sender_retries = True


def verify(req: Request, secrets: Secrets) -> bool:
    return G.verify_push(req, secrets["push_token"])


def payload(req: Request) -> dict:
    msg = G.push_message(req)
    inner = msg["data"]
    addr = inner.get("emailAddress")
    if not isinstance(addr, str) or "@" not in addr:
        raise BadPayload("notification has no emailAddress")
    history = inner.get("historyId")
    if isinstance(history, bool) or not isinstance(history, (str, int)) or str(history) == "":
        raise BadPayload("notification has no historyId")
    out = {"emailAddress": addr.strip().lower(), "historyId": str(history)}
    for f in ("messageId", "publishTime"):
        if f in msg:
            out[f] = msg[f]
    if "subscription" in msg:
        out["subscription"] = msg["subscription"]
    return out


def key(payload: dict, req: Request) -> str:
    return payload["emailAddress"]


def dedup_id(payload: dict, req: Request) -> str | None:
    return payload.get("messageId")


def event_type(payload: dict, req: Request) -> str:
    return "history"


def attrs(payload: dict, req: Request) -> dict:
    return {"account": payload["emailAddress"]}
