"""Test package source: a shared token in a header, key = the body's account."""
from hookspool.plugin import Request, Secrets, verify as v

name = "digest"
secrets = ("token", "cred")
auth_headers = ("X-Token",)


def verify(req: Request, secrets: Secrets) -> bool:
    return v.static_token(req.header("X-Token"), secrets["token"])


def key(payload: dict, req: Request) -> str:
    return str(payload.get("account") or "default")
