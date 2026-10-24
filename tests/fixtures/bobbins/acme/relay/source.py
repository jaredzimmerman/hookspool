"""Test package source: HMAC over the body, key = the body's id."""
from hookspool.plugin import BadPayload, Request, Secrets, verify as v

name = "relay"
secrets = ("secret",)
auth_headers = ("X-Signature",)


def verify(req: Request, secrets: Secrets) -> bool:
    return v.hmac_prefixed(req.header("X-Signature"), "sha256=", secrets["secret"], req.body)


def key(payload: dict, req: Request) -> str:
    if not isinstance(payload, dict) or not payload.get("id"):
        raise BadPayload("no id")
    return str(payload["id"])


def event_type(payload: dict, req: Request) -> str:
    return str(payload.get("type") or "")
