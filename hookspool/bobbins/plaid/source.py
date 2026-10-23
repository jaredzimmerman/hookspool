"""Plaid webhooks (https://plaid.com/docs/api/webhooks/webhook-verification/).

Plaid signs every webhook with an ES256 JWT in the ``Plaid-Verification`` header. The
JWT's ``kid`` names a public key fetched from Plaid's ``/webhook_verification_key/get``
(authenticated with the client id and secret, through ``hookspool/net.py``). Verification, in this order:

1. structure and ``alg == ES256`` and a short ``kid``, offline (``verify.jwt_header``);
   anything else is refused before any key lookup or network call;
2. the key for that ``kid``, from ``hookspool.verify.KeyCache`` (the shared key cache and fetch guard):
   known keys are cached (24 h); unknown ``kid`` s are
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

Setting ``env`` (the source's config key ``env``) = ``production`` (default), ``development`` or ``sandbox``. Secrets:
``client_id`` and ``secret`` (used only to fetch verification keys; an unreadable ref
answers 503 rather than 401, once the header has a valid structure).
"""
from __future__ import annotations

from hookspool.plugin import Request, Secrets, dedup
from hookspool.plugin import verify as v

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
KEY_TTL_S = v.KEY_TTL_S
NEGATIVE_TTL_S = v.KEY_NEGATIVE_TTL_S
FETCH_COOLDOWN_S = v.KEY_FETCH_COOLDOWN_S
DEDUP_BUCKET_S = 600
FETCH_TIMEOUT_S = 5
MAX_RESPONSE_BYTES = 64 * 1024
UNKNOWN_KEY_CODE = "INVALID_WEBHOOK_VERIFICATION_KEY_ID"   # plaid.com/docs/errors/invalid-input/


def _unknown_key(status: int | None, code: str) -> bool:
    """Plaid says it does not know the key id: 404, or 400 ``INVALID_WEBHOOK_VERIFICATION_KEY_ID``. Every
    other refusal, including 400 ``INVALID_API_KEYS`` (bad client credentials), is not an answer about the
    key, so a credential problem never drops or negative-caches a known key."""
    return status == 404 or (status == 400 and code == UNKNOWN_KEY_CODE)


def _key_expired(jwk: dict) -> bool:
    return jwk.get("expired_at") is not None


def fetch_key(env: str, client_id: str, secret: str, kid: str) -> dict | None:
    """Ask Plaid for the verification key ``kid``. Returns the JWK dict, or None only when Plaid says it does
    not know the key. Raises on network errors and on any other refusal. Replaceable: tests inject a stub."""
    if env not in ENVS:
        raise ValueError("plaid env must be one of " + ", ".join(ENVS))
    fetch = v.key_fetcher(f"https://{env}.plaid.com/webhook_verification_key/get", method="POST",
                          body=lambda k: {"client_id": client_id, "secret": secret, "key_id": k}, field="key",
                          unknown=_unknown_key, timeout_s=FETCH_TIMEOUT_S, max_bytes=MAX_RESPONSE_BYTES,
                          label="plaid-key")
    return fetch(kid)


def _cache_name(env: str) -> str:
    return f"plaid {env}"


def reset() -> None:
    """Forget every cached key (tests)."""
    v.reset_shared_key_caches("plaid ")


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
    # one cache per env, shared by every Plaid source in it (the keys are Plaid's, not per-credential)
    cache = v.shared_key_cache(_cache_name(env), ttl_s=KEY_TTL_S, negative_ttl_s=NEGATIVE_TTL_S,
                               cooldown_s=FETCH_COOLDOWN_S, usable=lambda jwk: not _key_expired(jwk))

    def get_key(kid: str):
        return cache.lookup(kid, req.now, fetch=lambda k: fetch_key(env, client_id, secret, k))

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
