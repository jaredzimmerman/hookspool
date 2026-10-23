"""Kit descriptions of the example plugins in ``tests/kitplugins`` (test secrets only).

The signing functions are independent of ``hookspool.verify``: they compute each
sender's signature with ``hmac``/``hashlib`` directly, so a bug in a verify helper cannot
hide behind a matching bug in a signer.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

import sourcekit
from kitplugins import headonly, signed, stamped
from sourcekit import SENTINEL, Kit, request

SIGNED_SECRET = "test-kit-secret-signed-" + SENTINEL
STAMPED_SECRET = "test-kit-secret-stamped-" + SENTINEL
HEAD_SECRET = "test-kit-secret-head-" + SENTINEL
CALLBACK_URL = "https://hooks.example.test/hooks/kithead"


def sign_signed(raw, secrets, now):
    mac = hmac.new(secrets["secret"].encode(), raw.body, hashlib.sha256).hexdigest()
    return raw.set("X-Kit-Signature", "sha256=" + mac)


def sign_stamped(raw, secrets, now):
    ts = str(int(now))
    mac = hmac.new(secrets["secret"].encode(), f"v0:{ts}:".encode() + raw.body, hashlib.sha256).hexdigest()
    return raw.set("X-Kit-Timestamp", ts).set("X-Kit-Signature", "v0=" + mac)


def sign_head(raw, secrets, now):
    mac = hmac.new(secrets["secret"].encode(), raw.body + CALLBACK_URL.encode(), hashlib.sha1).digest()
    return raw.set("X-Kit-Webhook", base64.b64encode(mac).decode())


def _json(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


SIGNED = Kit(
    name="kitsigned", module=signed, secrets={"secret": SIGNED_SECRET}, sign=sign_signed,
    sample=request("POST", "/hooks/kitsigned", {"Content-Type": "application/json", "X-Kit-Event": "push",
                                                "X-Kit-Delivery": "d-1"},
                   _json({"repo": "octo/widgets", "note": SENTINEL})),
)

STAMPED = Kit(
    name="kitstamped", module=stamped, secrets={"secret": STAMPED_SECRET}, sign=sign_stamped, timestamped=True,
    sample=request("POST", "/hooks/kitstamped", {"Content-Type": "application/json"},
                   _json({"event": "meeting.ended", "event_ts": 1799999999000,
                          "payload": {"object": {"uuid": "m-1", "topic": SENTINEL}}})),
    handshake=request("POST", "/hooks/kitstamped", {"Content-Type": "application/json"},
                      _json({"event": "endpoint.url_validation", "payload": {"plainToken": "plain-" + SENTINEL}})),
)

HEAD = Kit(
    name="kithead", module=headonly, secrets={"secret": HEAD_SECRET}, sign=sign_head,
    source_cfg={"callback_url": CALLBACK_URL},
    sample=request("POST", "/hooks/kithead", {"Content-Type": "application/json"},
                   _json({"model": {"id": "board-1"}, "action": {"id": "a-1", "text": SENTINEL}})),
    unauth_handshake=request("HEAD", "/hooks/kithead", {"User-Agent": "kit"}),
)

ALL = {"kitsigned": SIGNED, "kitstamped": STAMPED, "kithead": HEAD}
__all__ = ["ALL", "SIGNED", "STAMPED", "HEAD", "sourcekit"]
