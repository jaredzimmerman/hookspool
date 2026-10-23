"""Kit fixture source: a static token in X-Kitproc-Token."""
from hookspool.plugin import Request, Secrets, verify as v

name = "kitproc"
secrets = ("token",)
auth_headers = ("X-Kitproc-Token",)


def verify(req: Request, secrets: Secrets) -> bool:
    return v.static_token(req.header("X-Kitproc-Token"), secrets["token"])


def key(payload: dict, req: Request) -> str:
    return str(payload.get("account") or "default")
