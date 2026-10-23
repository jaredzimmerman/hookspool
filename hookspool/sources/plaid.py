"""Plaid webhooks (https://plaid.com/docs/api/webhooks/webhook-verification/).

Plaid signs every webhook with an ES256 JWT in the ``Plaid-Verification`` header. The
JWT's ``kid`` names a public key fetched from Plaid's ``/webhook_verification_key/get``
(authenticated with the client id and secret). Verification, in this order:

1. structure and ``alg == ES256`` and a short ``kid``, offline (``verify.jwt_header``);
   anything else is refused before any key lookup or network call;
2. the key for that ``kid``: known keys are cached (24 h); unknown ``kid`` s are
   negative-cached for 10 min; at most ONE key fetch per minute per ``env``, so
   unauthenticated requests with random ``kid`` s cannot amplify against Plaid's API or tie
   up intake threads (a request that needs a fetch inside the cooldown is answered 401 and
   Plaid retries it with backoff, so a genuine rotation is picked up within a minute). A
   known key whose 24 h TTL has run out is still served while a refresh is blocked by the
   cooldown or fails, so an attacker holding the fetch slot cannot starve genuine webhooks;
   it is dropped only when a successful fetch says the key is unknown or expired
   (``expired_at`` set). The cache is shared by every Plaid source in the same ``env``
   (the keys are Plaid's public keys, not per-credential). Limit, from the contract: a
   ``kid`` never cached (a key rotation, or the first key after an intake restart) still
   competes for the one fetch a minute, so a client sending random-``kid`` requests about once
   a second can hold that key at 401 until it stops or Plaid stops retrying. The deploy
   README (T7) records this; the proxy ``limit_req`` slows it per client address;
3. the ES256 signature, ``iat`` within 5 minutes either side of now, and the
   ``request_body_sha256`` claim equal to the SHA-256 of the raw body
   (``verify.jwt_es256``).

``key = item_id`` (``link_token`` for Link events, which have no item; the webhook type
otherwise). Plaid payloads carry no id and a ``SYNC_UPDATES_AVAILABLE`` or ``DEFAULT_UPDATE``
body is byte-identical for every notification on an item, so ``dedup = SHA-256 of the body
plus the 600 s time bucket it arrived in``. The plugin enforces the C4 window itself, with no
config line to forget: a retry of one delivery inside its bucket is a duplicate, an identical
body in a later bucket is a new event. At a bucket edge a retry can be delivered twice
(duplication, never loss).
``span = "sync"`` groups bank backfills. Plaid retries failed webhooks with backoff over
hours (``sender_retries = True``).

Config keys: ``env`` = ``production`` (default), ``development`` or ``sandbox``. Secrets:
``client_id`` and ``secret`` (used only to fetch verification keys; an unreadable ref
answers 503 rather than 401, once the header has a valid structure).
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request

from hookspool.plugin import Request, Secrets, dedup
from hookspool.plugin import verify as v

log = logging.getLogger("hookspool.sources.plaid")

name = "plaid"
secrets = ("client_id", "secret")
auth_headers = ("Plaid-Verification",)
keep_headers = ()
config_keys = ("env",)
span = "sync"
expect_every_s = None
sender_retries = True
action = "hermes:plaid"

ENVS = ("production", "development", "sandbox")
KEY_TTL_S = 24 * 3600
NEGATIVE_TTL_S = 600
FETCH_COOLDOWN_S = 60
DEDUP_BUCKET_S = 600
FETCH_TIMEOUT_S = 5
MAX_CACHED = 256
MAX_RESPONSE_BYTES = 64 * 1024
MAX_ERROR_BYTES = 4096
UNKNOWN_KEY_CODE = "INVALID_WEBHOOK_VERIFICATION_KEY_ID"   # plaid.com/docs/errors/invalid-input/


def _error_code(e) -> str:
    """Plaid's ``error_code`` from an HTTP error body, or "" (only ``A-Z0-9_``, at most 64 chars,
    so nothing else from the body can reach a log line)."""
    try:
        data = json.loads(e.read(MAX_ERROR_BYTES))
    except Exception:  # noqa: BLE001 - an unreadable error body just has no code
        return ""
    code = data.get("error_code") if isinstance(data, dict) else None
    if not isinstance(code, str) or len(code) > 64 or not code or any(
            c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for c in code):
        return ""
    return code


def fetch_key(env: str, client_id: str, secret: str, kid: str) -> dict | None:
    """Ask Plaid for the verification key ``kid``. Returns the JWK dict, or None only when
    Plaid says it does not know the key (404, or 400 ``INVALID_WEBHOOK_VERIFICATION_KEY_ID``).
    Raises on network errors and on any other refusal, including 400 ``INVALID_API_KEYS`` (bad
    client credentials), so a credential problem never drops or negative-caches a known key.
    Replaceable: tests inject a stub."""
    if env not in ENVS:
        raise ValueError("plaid env must be one of " + ", ".join(ENVS))
    body = json.dumps({"client_id": client_id, "secret": secret, "key_id": kid}).encode("utf-8")
    req = urllib.request.Request(f"https://{env}.plaid.com/webhook_verification_key/get", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as r:
            data = json.loads(r.read(MAX_RESPONSE_BYTES))
    except urllib.error.HTTPError as e:
        code = _error_code(e)
        # Logged once per fetch (a fetch happens at most once a minute). Status and Plaid's
        # error code only: no body, no credentials.
        log.warning("plaid key fetch refused: env=%s http=%s error_code=%s", env, e.code, code or "-")
        if e.code == 404 or (e.code == 400 and code == UNKNOWN_KEY_CODE):
            return None                       # Plaid says it does not know this key id
        raise                                 # INVALID_API_KEYS and the rest: not an answer about the key
    jwk = data.get("key") if isinstance(data, dict) else None
    return jwk if isinstance(jwk, dict) else None


class KeyCache:
    """Known keys, unknown-kid negative cache and the fetch cooldown for one ``env``."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.keys: dict[str, tuple[float, dict]] = {}      # kid -> (expires, jwk)
        self.unknown: dict[str, float] = {}                # kid -> expires
        self.last_fetch_at = float("-inf")

    def _prune(self, now: float) -> None:
        for table in (self.keys, self.unknown):
            if len(table) > MAX_CACHED:
                for k in [k for k, e in table.items() if (e[0] if isinstance(e, tuple) else e) <= now]:
                    del table[k]
                while len(table) > MAX_CACHED:
                    del table[next(iter(table))]

    def lookup(self, kid: str, now: float, fetch) -> dict | None:
        """The JWK for ``kid`` or None. ``fetch(kid)`` is called at most once per
        cooldown, outside the lock."""
        with self.lock:
            hit = self.keys.get(kid)
            if hit is not None and hit[0] > now:
                return hit[1]
            stale = hit[1] if hit is not None else None          # past its TTL, still the best we know
            if self.unknown.get(kid, float("-inf")) > now:
                return None
            if now - self.last_fetch_at < FETCH_COOLDOWN_S and now >= self.last_fetch_at:
                return stale                                     # refresh blocked: keep serving the known key
            self.last_fetch_at = now
        try:
            jwk = fetch(kid)
        except Exception as e:  # noqa: BLE001 - a failed fetch keeps the known key; Plaid retries otherwise
            log.warning("plaid key fetch failed: %s", e.__class__.__name__)
            return stale
        with self.lock:
            if jwk is None or jwk.get("expired_at") is not None:
                self.unknown[kid] = now + NEGATIVE_TTL_S
                self.keys.pop(kid, None)
                jwk = None
            else:
                self.keys[kid] = (now + KEY_TTL_S, jwk)
                self.unknown.pop(kid, None)
            self._prune(now)
        return jwk


_caches: dict[str, KeyCache] = {}
_caches_lock = threading.Lock()


def reset() -> None:
    """Forget every cached key (tests)."""
    with _caches_lock:
        _caches.clear()


def _cache(env: str) -> KeyCache:
    with _caches_lock:
        return _caches.setdefault(env, KeyCache())


def _env(req: Request) -> str:
    env = str(req.config.get("env") or "production")
    if env not in ENVS:
        raise ValueError("plaid env must be one of " + ", ".join(ENVS))
    return env


def verify(req: Request, secrets: Secrets) -> bool:
    token = req.header("Plaid-Verification")
    if v.jwt_header(token) is None:                      # structure and alg first, offline
        return False
    env = _env(req)
    client_id, secret = secrets["client_id"], secrets["secret"]   # unreadable ref -> SecretError -> 503
    cache = _cache(env)

    def get_key(kid: str):
        return cache.lookup(kid, req.now, lambda k: fetch_key(env, client_id, secret, k))

    return v.jwt_es256(token, get_key, body=req.body, now=req.now)


def _s(payload: dict, name_: str) -> str:
    val = payload.get(name_)
    return val if isinstance(val, str) else ""


def key(payload: dict, req: Request) -> str:
    return _s(payload, "item_id") or _s(payload, "link_token") or _s(payload, "webhook_type") or "plaid"


def dedup_id(payload: dict, req: Request) -> str:
    """Body hash plus the DEDUP_BUCKET_S bucket it arrived in (the C4 window, enforced here)."""
    return f"{dedup.body_sha256(payload, req)}:{int(req.now // DEDUP_BUCKET_S)}"


def event_type(payload: dict, req: Request) -> str:
    t, c = _s(payload, "webhook_type"), _s(payload, "webhook_code")
    return f"{t}.{c}" if t and c else t or c


def attrs(payload: dict, req: Request) -> dict:
    out = {"webhook_type": _s(payload, "webhook_type"), "webhook_code": _s(payload, "webhook_code")}
    if _s(payload, "environment"):
        out["environment"] = _s(payload, "environment")
    return out
