"""Kit plugin: a hex HMAC-SHA256 header with a prefix (the GitHub shape)."""
from hookspool.plugin import Request, Secrets, dedup, verify as v

name = "kitsigned"
secrets = ("secret",)
auth_headers = ("X-Kit-Signature",)
keep_headers = ("X-Kit-Event", "X-Kit-Delivery")
span = "burst"
sender_retries = False
action = "hermes:kit"

SIG = "X-Kit-Signature"


def verify(req: Request, secrets: Secrets) -> bool:
    return v.hmac_prefixed(req.header(SIG), "sha256=", secrets["secret"], req.body)


def ignore(req: Request) -> str | None:
    return "ping" if req.header("X-Kit-Event") == "ping" else None


def key(payload: dict, req: Request) -> str:
    return f"{payload['repo']}:{req.header('X-Kit-Event') or 'event'}"


dedup_id = dedup.header("X-Kit-Delivery")


def event_type(payload: dict, req: Request) -> str:
    return req.header("X-Kit-Event")


def attrs(payload: dict, req: Request) -> dict:
    return {"repo": payload["repo"]}
