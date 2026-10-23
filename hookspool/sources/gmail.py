"""Gmail push notifications via Google Cloud Pub/Sub (https://developers.google.com/gmail/api/guides/push).

A Gmail ``users.watch`` publishes ``{"emailAddress": ..., "historyId": ...}`` to a Pub/Sub topic; a
push subscription POSTs each message to this endpoint as the usual Pub/Sub envelope::

    {"message": {"data": "<base64 JSON>", "messageId": "123", "publishTime": "..."},
     "subscription": "projects/p/subscriptions/s"}

Verification is the push token: Pub/Sub cannot sign a push, so the subscription's endpoint URL
carries a long random ``?token=...`` that only our subscription knows (an ``X-Push-Token`` header
is accepted too, for a proxy that moves it). It is compared in constant time. The query string is
never logged or stored. Hardening for later: verify the OIDC ``Authorization`` token Pub/Sub can
attach (ARCHITECTURE section 10).

``payload`` is the decoded notification, normalised to ``{emailAddress, historyId, messageId,
publishTime, subscription}`` (``emailAddress`` lower-cased). A push whose envelope does not
decode to that is not silently dropped: ``payload`` raises ``BadPayload`` and the intake
quarantines the request for an operator.

``key = emailAddress``, so one mailbox's pushes coalesce into one batch and the delta script
reads the mailbox once for all of them (the ``mail`` span, 30 s quiet / 120 s max).
``dedup = messageId``. A Pub/Sub ``messageId`` is per push, not per mailbox change: if the intake
is killed after COMMIT and before answering, Pub/Sub redelivers a push with a NEW ``messageId``,
which joins the batch as a second event and is reconciled by the script (it reads Gmail's history
from its stored ``historyId``, so extra pushes cost nothing and lose nothing). Pub/Sub retries a
non-2xx for the subscription's retention (``sender_retries``).

Action: ``script:gmail_delta`` (``deploy/scripts/gmail_delta.py --batch``), which decides whether
a message needs the agent and emits a ``hermes:`` event if so.

Secret: ``push_token``.
"""
from __future__ import annotations

import base64
import binascii
import json

from hookspool.plugin import BadPayload, Request, Secrets, verify as v

name = "gmail"
secrets = ("push_token",)
auth_headers = ("X-Push-Token",)
keep_headers = ()
span = "mail"
expect_every_s = None
sender_retries = True
action = "script:gmail_delta"

TOKEN_PARAM = "token"


def verify(req: Request, secrets: Secrets) -> bool:
    return v.static_token(req.query.get(TOKEN_PARAM) or req.header("X-Push-Token"), secrets["push_token"])


def _decode(data: object) -> dict:
    if not isinstance(data, str) or not data:
        raise BadPayload("pub/sub message has no data")
    try:
        raw = base64.b64decode(data + "=" * (-len(data) % 4), validate=False)
        inner = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise BadPayload("pub/sub data is not base64 JSON") from None
    if not isinstance(inner, dict):
        raise BadPayload("pub/sub data is not an object")
    return inner


def payload(req: Request) -> dict:
    env = req.json()
    msg = env.get("message") if isinstance(env, dict) else None
    if not isinstance(msg, dict):
        raise BadPayload("not a pub/sub push envelope")
    inner = _decode(msg.get("data"))
    addr = inner.get("emailAddress")
    if not isinstance(addr, str) or "@" not in addr:
        raise BadPayload("notification has no emailAddress")
    history = inner.get("historyId")
    if isinstance(history, bool) or not isinstance(history, (str, int)) or str(history) == "":
        raise BadPayload("notification has no historyId")
    out = {"emailAddress": addr.strip().lower(), "historyId": str(history)}
    for src, dst in (("messageId", "messageId"), ("publishTime", "publishTime")):
        if isinstance(msg.get(src), str) and msg[src]:
            out[dst] = msg[src]
    if isinstance(env.get("subscription"), str) and env["subscription"]:
        out["subscription"] = env["subscription"]
    return out


def key(payload: dict, req: Request) -> str:
    return payload["emailAddress"]


def dedup_id(payload: dict, req: Request) -> str | None:
    return payload.get("messageId")


def event_type(payload: dict, req: Request) -> str:
    return "history"


def attrs(payload: dict, req: Request) -> dict:
    return {"account": payload["emailAddress"]}
