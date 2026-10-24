"""Zoom event subscriptions (https://developers.zoom.us/docs/api/webhooks/).

Every delivery carries ``x-zm-request-timestamp`` (unix seconds) and ``x-zm-signature``, which is
``v0=`` followed by the hex HMAC-SHA256, keyed with the app's Secret Token, of
``v0:<timestamp>:<body>``. The window is two-sided, 300 s (``verify.timestamped``).

The bridge this plugin replaces could never verify a delivery: it compared the received
``v0=<digest>`` to a base64 digest that had no ``v0=`` prefix, so every event was answered 403 (and
Zoom's documented digest is hex, not base64). Only its URL validation ever worked. There is nothing
to stay compatible with, so only the documented scheme is accepted.

URL validation. When the endpoint is added Zoom POSTs ``endpoint.url_validation`` with a
``plainToken`` and expects ``{"plainToken", "encryptedToken"}`` back, where ``encryptedToken`` is
the hex HMAC-SHA256 of the plain token keyed with the same Secret Token. That answer is a signing
oracle for anyone who may ask, so it is given only AFTER ``verify`` succeeds (ARCHITECTURE
section 10): an unsigned or mis-signed validation request is a 401 with no body. Zoom signs the
validation request with the Secret Token; if a (very old) app version does not, add the endpoint
with the Secret Token already set, as Zoom's current documentation describes. A challenge never
enqueues.

``key`` is the meeting (or webinar) ``uuid`` so one meeting's events coalesce and stay in order;
events with no meeting fall back to ``payload.object.id``, then the account id, then the event
name. ``dedup = <event>:<event_ts>:<uuid or id>``. The ``immediate`` span: a meeting wake-up is not
delayed. Zoom retries a few times over a few minutes (``sender_retries``).

Secret: ``webhook_secret`` (the Secret Token of the app's Event Subscription).
"""
from __future__ import annotations

from hookspool.plugin import Request, Response, Secrets, verify as v

name = "zoom"
secrets = ("webhook_secret",)
auth_headers = ("x-zm-signature",)
keep_headers = ()
span = "immediate"
expect_every_s = None
sender_retries = True
action = "hermes:zoom"

VALIDATION = "endpoint.url_validation"


def verify(req: Request, secrets: Secrets) -> bool:
    return v.timestamped(req.header("x-zm-signature"), secrets["webhook_secret"], req.body, now=req.now,
                         timestamp=req.header("x-zm-request-timestamp"), version="v0", template="{v}:{t}:")


def challenge(req: Request, secrets: Secrets) -> Response | None:
    if req.method != "POST":
        return None
    try:
        doc = req.json()
    except ValueError:
        return None
    if not isinstance(doc, dict) or doc.get("event") != VALIDATION:
        return None
    obj = doc.get("payload")
    plain = obj.get("plainToken") if isinstance(obj, dict) else None
    if not isinstance(plain, str) or not plain:
        return Response.json(400, {"error": "missing plainToken"})
    return Response.json(200, {"plainToken": plain, "encryptedToken": v.hmac_hex_sign(secrets["webhook_secret"], plain)})


def _object(payload: dict) -> dict:
    p = payload.get("payload")
    o = p.get("object") if isinstance(p, dict) else None
    return o if isinstance(o, dict) else {}


def _ident(payload: dict) -> str | None:
    o = _object(payload)
    for k in ("uuid", "id"):
        if o.get(k) not in (None, ""):
            return str(o[k])
    return None


def key(payload: dict, req: Request) -> str:
    ident = _ident(payload)
    if ident:
        return ident
    acct = (payload.get("payload") or {}).get("account_id") if isinstance(payload.get("payload"), dict) else None
    return f"account:{acct}" if acct else f"event:{payload.get('event') or 'unknown'}"


def dedup_id(payload: dict, req: Request) -> str | None:
    ts, ident = payload.get("event_ts"), _ident(payload)
    if ts in (None, "") or not ident:
        return None
    return f"{payload.get('event') or ''}:{ts}:{ident}"


def event_type(payload: dict, req: Request) -> str:
    return str(payload.get("event") or "")


def attrs(payload: dict, req: Request) -> dict:
    ev = str(payload.get("event") or "")
    return {"event": ev, "kind": ev.split(".", 1)[0]}
