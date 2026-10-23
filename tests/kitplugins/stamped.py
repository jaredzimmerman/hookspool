"""Kit plugin: a timestamped ``v0=`` signature over ``v0:<ts>:<body>`` and a URL-validation
handshake whose answer is derived from the secret (the Zoom shape). The handshake runs
only after ``verify``: answering it unauthenticated would be a signing oracle."""
from hookspool.plugin import Request, Response, Secrets, verify as v

name = "kitstamped"
secrets = ("secret",)
auth_headers = ("X-Kit-Signature",)
keep_headers = ("X-Kit-Timestamp",)
span = "immediate"
sender_retries = True
action = "hermes:kit-meetings"


def verify(req: Request, secrets: Secrets) -> bool:
    return v.timestamped(req.header("X-Kit-Signature"), secrets["secret"], req.body, now=req.now,
                         timestamp=req.header("X-Kit-Timestamp"), version="v0", template="{v}:{t}:")


def challenge(req: Request, secrets: Secrets) -> Response | None:
    p = req.json()
    if not isinstance(p, dict) or p.get("event") != "endpoint.url_validation":
        return None
    plain = str((p.get("payload") or {}).get("plainToken") or "")
    return Response.json(200, {"plainToken": plain, "encryptedToken": v.hmac_hex_sign(secrets["secret"], plain)})


def key(payload: dict, req: Request) -> str:
    return payload["payload"]["object"]["uuid"]


def dedup_id(payload: dict, req: Request) -> str | None:
    return f"{payload.get('event_ts')}:{payload['payload']['object']['uuid']}"


def event_type(payload: dict, req: Request) -> str:
    return payload.get("event", "")
