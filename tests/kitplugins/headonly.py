"""Kit plugin: base64 HMAC-SHA1 over body + callback URL, and a ``HEAD`` handshake with
no signature that must be answered with a constant empty 200 (the Trello shape)."""
from hookspool.plugin import Request, Response, Secrets, dedup, verify as v

name = "kithead"
secrets = ("secret",)
auth_headers = ("X-Kit-Webhook",)
config_keys = ("callback_url",)
challenge_unauthenticated = True
span = "mail"
action = "hermes:kit-boards"


def verify(req: Request, secrets: Secrets) -> bool:
    cb = str(req.config.get("callback_url") or "")
    return bool(cb) and v.hmac_b64(req.header("X-Kit-Webhook"), secrets["secret"], req.body + cb.encode("utf-8"),
                                   algo="sha1")


def challenge(req: Request, secrets: Secrets) -> Response | None:
    return Response(200) if req.method == "HEAD" else None


def key(payload: dict, req: Request) -> str:
    return payload["model"]["id"]


dedup_id = dedup.field("action.id")
