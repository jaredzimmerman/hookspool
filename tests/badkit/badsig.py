"""A deliberately BAD source plugin, used only to prove the conformance kit and CI reject one.

``verify`` accepts every request (a missing or wrong signature passes), ``key`` reads a field the
payload may not have, and nothing here is safe to deploy. It is not registered anywhere.
"""
from hookspool.plugin import Request, Secrets

name = "badsig"
secrets = ("secret",)
auth_headers = ("X-Kit-Signature",)
keep_headers = ("X-Kit-Event",)
span = "burst"
sender_retries = False
action = "hermes:kit"


def verify(req: Request, secrets: Secrets) -> bool:
    return True                       # the defect: no signature check at all


def key(payload: dict, req: Request) -> str:
    return payload["repo"]
