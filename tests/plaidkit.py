"""Plaid test fixtures: a deterministic ES256 test key (derived from a label, so no key
material is committed), its JWK, a signer that is independent of ``hookspool.verify`` and
the Kit description for the shared matrix. Test values only."""
from __future__ import annotations

import base64
import hashlib
import json

import jwt
import sourcekit
from cryptography.hazmat.primitives.asymmetric import ec
from sourcekit import SENTINEL, Kit, request

CLIENT_ID = "test-plaid-client-" + SENTINEL
SECRET = "test-plaid-secret-" + SENTINEL
KID = "test-kid-0001"
ENV = "sandbox"


def private_key(label: str = "hookspool-test-plaid-key-1"):
    n = int.from_bytes(hashlib.sha256(label.encode()).digest(), "big")
    n = n % (2 ** 255) or 1                                              # well inside the curve order
    return ec.derive_private_key(n, ec.SECP256R1())


def jwk_of(key, kid: str = KID, expired_at=None) -> dict:
    nums = key.public_key().public_numbers()

    def b64(i: int) -> str:
        return base64.urlsafe_b64encode(i.to_bytes(32, "big")).rstrip(b"=").decode()
    return {"alg": "ES256", "created_at": 1_700_000_000, "crv": "P-256", "expired_at": expired_at, "kid": kid,
            "kty": "EC", "use": "sig", "x": b64(nums.x), "y": b64(nums.y)}


def make_jwt(body: bytes, now: float, *, key=None, kid: str = KID, alg: str = "ES256", extra: dict | None = None,
             claims: dict | None = None) -> str:
    c = {"iat": int(now), "request_body_sha256": hashlib.sha256(body).hexdigest()}
    c.update(claims or {})
    c.update(extra or {})
    return jwt.encode(c, key or private_key(), algorithm=alg, headers={"kid": kid, "typ": "JWT"})


def sign(raw, secrets, now):
    return raw.set("Plaid-Verification", make_jwt(raw.body, now))


BODY = json.dumps({"webhook_type": "TRANSACTIONS", "webhook_code": "SYNC_UPDATES_AVAILABLE",
                   "item_id": "item-abc123", "environment": ENV, "note": SENTINEL}, separators=(",", ":")).encode()


def kit(module) -> Kit:
    return Kit(name="plaid", module=module, secrets={"client_id": CLIENT_ID, "secret": SECRET}, sign=sign,
               timestamped=True, source_cfg={"env": ENV},
               sample=request("POST", "/hooks/plaid", {"Content-Type": "application/json"}, BODY))


__all__ = ["sourcekit"]
