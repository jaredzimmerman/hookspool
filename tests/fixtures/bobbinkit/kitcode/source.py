"""Kit fixture source: HMAC over the body in X-Kitcode-Signature."""
from hookspool.plugin import BadPayload, Request, Secrets, verify as v

name = "kitcode"
secrets = ("secret",)
auth_headers = ("X-Kitcode-Signature",)


def verify(req: Request, secrets: Secrets) -> bool:
    return v.hmac_prefixed(req.header("X-Kitcode-Signature"), "sha256=", secrets["secret"], req.body)


def key(payload: dict, req: Request) -> str:
    if not isinstance(payload, dict) or not payload.get("id"):
        raise BadPayload("no id")
    return str(payload["id"])


def dedup_id(payload: dict, req: Request) -> str | None:
    return str(payload.get("id") or "") or None


def event_type(payload: dict, req: Request) -> str:
    return str(payload.get("type") or "")
