"""Signature verification: the only comparison API source plugins are given (ARCHITECTURE §10).

Every check here ends in ``hmac.compare_digest`` (through ``equal``); nothing in hookspool
compares a signature, token or digest any other way (a test greps for it). Every helper
returns ``False`` for missing, malformed or wrong input and never raises on what a sender
controls, so a plugin's ``verify`` can be a single expression.

Schemes:

- ``hmac_hex``        hex HMAC of a message (Kaneo ``X-Kaneo-Signature``)
- ``hmac_b64``        base64 HMAC of a message (Trello: SHA-1 over body + callback URL)
- ``hmac_prefixed``   ``<prefix><hex or base64>`` (GitHub ``sha256=<hex>``)
- ``timestamped``     a signed timestamp: ``t=<ts>,v1=<sig>`` in one header (Stripe style), or
                      a separate timestamp header with ``<version>=<sig>`` (Zoom ``v0``)
- ``standard_webhooks`` ``webhook-id`` / ``webhook-timestamp`` / ``webhook-signature``
- ``static_token``    a shared token (Pub/Sub push URL token, Calendar channel token)
- ``jwt_es256``       an ES256 JWT whose claims carry the body's SHA-256 (Plaid)

A sender's published verification keys are looked up through ``KeyCache`` (TTL, negative
cache, one fetch per cooldown, stale key served while a refresh fails), with ``jwks_fetcher``
for a JWKS URL; a source never fetches keys itself (ARCHITECTURE 14.3.7).

Timestamped schemes take ``tolerance_s`` (default 300) and the check is two-sided,
``abs(now - t) <= tolerance_s``: a future-dated request is rejected like a stale one.
Pass ``now=req.now`` so tests can drive the clock.

The ``*_sign`` helpers produce the same signatures for outbound deliveries and tests.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import threading
import time
from typing import Any, Callable

DEFAULT_TOLERANCE_S = 300.0
MAX_SIGNATURE_CHARS = 4096
_ALGOS = {"sha1": hashlib.sha1, "sha256": hashlib.sha256, "sha384": hashlib.sha384,
          "sha512": hashlib.sha512}

__all__ = ["DEFAULT_TOLERANCE_S", "equal", "hmac_digest", "sha256_hex", "within_tolerance",
           "hmac_hex", "hmac_b64", "hmac_prefixed", "timestamped", "standard_webhooks",
           "standard_webhooks_key", "standard_webhooks_sign", "static_token", "jwt_header",
           "jwt_es256", "hmac_hex_sign", "hmac_b64_sign", "KeyCache", "jwks_fetcher"]


# ---- primitives ---------------------------------------------------------------------------
def _bytes(x: Any) -> bytes:
    if x is None:
        return b""
    if isinstance(x, str):
        return x.encode("utf-8")
    if isinstance(x, (bytes, bytearray, memoryview)):
        return bytes(x)
    raise TypeError("expected str or bytes")


def equal(a: Any, b: Any) -> bool:
    """Constant-time equality of two strings or byte strings.

    ``False`` when either side is empty or missing, so an unset secret or header can never
    match an empty signature."""
    try:
        x, y = _bytes(a), _bytes(b)
    except TypeError:
        return False
    if not x or not y:
        return False
    return hmac.compare_digest(x, y)


def hmac_digest(secret: str | bytes, message: str | bytes, algo: str = "sha256") -> bytes:
    try:
        h = _ALGOS[algo]
    except KeyError:
        raise ValueError(f"unsupported HMAC algorithm {algo!r}") from None
    return hmac.new(_bytes(secret), _bytes(message), h).digest()


def sha256_hex(data: str | bytes) -> str:
    return hashlib.sha256(_bytes(data)).hexdigest()


def within_tolerance(ts: Any, now: float | None = None, tolerance_s: float = DEFAULT_TOLERANCE_S) -> bool:
    """Two-sided timestamp window: ``abs(now - ts) <= tolerance_s``. ``ts`` is integer
    seconds (a string of digits or an int)."""
    t = _int_ts(ts)
    if t is None:
        return False
    now = time.time() if now is None else float(now)
    return abs(now - t) <= float(tolerance_s)


def _int_ts(ts: Any) -> int | None:
    if isinstance(ts, bool):
        return None
    if isinstance(ts, int):
        return ts
    if isinstance(ts, float) and ts.is_integer():
        return int(ts)
    if isinstance(ts, (str, bytes)):
        s = ts.decode("ascii", "replace") if isinstance(ts, bytes) else ts
        s = s.strip()
        if s.isascii() and s.isdigit() and 0 < len(s) <= 12:
            return int(s)
    return None


def _usable(provided: Any) -> str | None:
    if isinstance(provided, bytes):
        provided = provided.decode("ascii", "replace")
    if not isinstance(provided, str):
        return None
    provided = provided.strip()
    if not provided or len(provided) > MAX_SIGNATURE_CHARS:
        return None
    return provided


def _b64decode(s: str, *, urlsafe: bool = False) -> bytes | None:
    s = s.strip()
    if not s:
        return None
    s += "=" * (-len(s) % 4)
    try:
        if urlsafe:
            return base64.b64decode(s.encode("ascii"), altchars=b"-_", validate=True)
        return base64.b64decode(s.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        return None


def _match_encoded(provided: str, expected: bytes, encoding: str) -> bool:
    if encoding == "hex":
        return equal(provided.lower(), expected.hex())
    if encoding in ("base64", "b64", "base64url"):
        got = _b64decode(provided, urlsafe=encoding == "base64url")
        return got is not None and equal(got, expected)
    raise ValueError(f"unsupported signature encoding {encoding!r}")


# ---- HMAC schemes -------------------------------------------------------------------------------
def hmac_hex(provided: Any, secret: str | bytes, message: str | bytes, *, algo: str = "sha256") -> bool:
    """``provided`` is the hex HMAC of ``message`` (case-insensitive)."""
    p = _usable(provided)
    if p is None or not _bytes(secret):
        return False
    return _match_encoded(p, hmac_digest(secret, message, algo), "hex")


def hmac_b64(provided: Any, secret: str | bytes, message: str | bytes, *, algo: str = "sha256",
             urlsafe: bool = False) -> bool:
    """``provided`` is the base64 HMAC of ``message`` (padding optional)."""
    p = _usable(provided)
    if p is None or not _bytes(secret):
        return False
    return _match_encoded(p, hmac_digest(secret, message, algo), "base64url" if urlsafe else "base64")


def hmac_prefixed(provided: Any, prefix: str, secret: str | bytes, message: str | bytes, *,
                  algo: str = "sha256", encoding: str = "hex") -> bool:
    """``provided`` is ``prefix`` followed by the HMAC (GitHub: ``sha256=<hex>``)."""
    p = _usable(provided)
    if p is None or not prefix or not p.startswith(prefix) or not _bytes(secret):
        return False
    return _match_encoded(p[len(prefix):], hmac_digest(secret, message, algo), encoding)


def timestamped(signature: Any, secret: str | bytes, body: bytes, *, now: float | None = None,
                timestamp: Any = None, tolerance_s: float = DEFAULT_TOLERANCE_S, version: str = "v1",
                template: str = "{t}.", algo: str = "sha256", encoding: str = "hex") -> bool:
    """A signature over a timestamp and the body, with a two-sided time window.

    With ``timestamp=None`` the header carries both: ``t=<ts>,v1=<sig>[,v1=<sig>…]``
    (several signatures are allowed during secret rotation). With ``timestamp`` given (a
    separate header), ``signature`` is ``<version>=<sig>``. The signed message is
    ``template.format(t=ts, v=version)`` followed by the body: ``"{t}."`` for the ``t=,v1=``
    style, ``"{v}:{t}:"`` for Zoom's ``v0``.
    """
    sig = _usable(signature)
    if sig is None or not _bytes(secret):
        return False
    if timestamp is None:
        ts, cands = None, []
        for part in sig.split(","):
            k, sep, v = part.strip().partition("=")
            if not sep:
                continue
            if k == "t" and ts is None:
                ts = v.strip()
            elif k == version and v.strip():
                cands.append(v.strip())
    else:
        ts = timestamp.decode("ascii", "replace") if isinstance(timestamp, bytes) else timestamp
        ts = ts.strip() if isinstance(ts, str) else ts
        pre = f"{version}="
        cands = [sig[len(pre):]] if sig.startswith(pre) and len(sig) > len(pre) else []
    t = _int_ts(ts)
    if t is None or not cands or not within_tolerance(t, now, tolerance_s):
        return False
    expected = hmac_digest(secret, template.format(t=t, v=version).encode("utf-8") + _bytes(body), algo)
    ok = False
    for c in cands[:8]:
        ok = _match_encoded(c, expected, encoding) or ok
    return ok


# ---- Standard Webhooks ---------------------------------------------------------------------------------
def standard_webhooks_key(secret: str | bytes) -> bytes:
    """The HMAC key: a ``whsec_<base64>`` secret is decoded, anything else is used raw."""
    s = _bytes(secret)
    if s.startswith(b"whsec_"):
        k = _b64decode(s[6:].decode("ascii", "replace"))
        if k is None:
            raise ValueError("whsec_ secret is not valid base64")
        return k
    return s


def standard_webhooks_sign(secret: str | bytes, msg_id: str, timestamp: int | str, body: bytes) -> str:
    """``v1,<base64 HMAC-SHA256(key, "<id>.<timestamp>.<body>")>``."""
    msg = f"{msg_id}.{int(timestamp)}.".encode("utf-8") + _bytes(body)
    sig = hmac.new(standard_webhooks_key(secret), msg, hashlib.sha256).digest()
    return "v1," + base64.b64encode(sig).decode("ascii")


def standard_webhooks(msg_id: Any, timestamp: Any, signature: Any, secret: str | bytes, body: bytes, *,
                      now: float | None = None, tolerance_s: float = DEFAULT_TOLERANCE_S) -> bool:
    """Standard Webhooks / Svix: ``signature`` is a space-separated list of ``v1,<base64>``."""
    sig = _usable(signature)
    mid = _usable(msg_id)
    t = _int_ts(timestamp)
    if sig is None or mid is None or t is None or not _bytes(secret):
        return False
    if not within_tolerance(t, now, tolerance_s):
        return False
    try:
        key = standard_webhooks_key(secret)
    except ValueError:
        return False
    expected = hmac.new(key, f"{mid}.{t}.".encode("utf-8") + _bytes(body), hashlib.sha256).digest()
    ok = False
    for part in sig.split()[:8]:
        ver, sep, val = part.partition(",")
        if sep and ver == "v1":
            got = _b64decode(val)
            ok = (got is not None and equal(got, expected)) or ok
    return ok


# ---- tokens ------------------------------------------------------------------------------------------
def static_token(provided: Any, expected: str | bytes) -> bool:
    """A shared token sent as is (URL token, channel token, shared-secret header)."""
    p = _usable(provided)
    return p is not None and equal(p, expected)


# ---- JWT (ES256) -------------------------------------------------------------------------------------------
def _b64url_json(seg: str) -> dict | None:
    raw = _b64decode(seg, urlsafe=True)
    if raw is None:
        return None
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def jwt_header(token: Any, *, alg: str = "ES256", max_bytes: int = MAX_SIGNATURE_CHARS) -> dict | None:
    """The unverified JOSE header of a compact JWT, or None unless the token has three
    base64url segments, a JSON header with ``alg == alg`` and a short string ``kid``.
    Cheap and offline: call it before any key lookup."""
    t = _usable(token)
    if t is None or len(t) > max_bytes or not t.isascii():
        return None
    parts = t.split(".")
    if len(parts) != 3 or not all(parts):
        return None
    h = _b64url_json(parts[0])
    if h is None or h.get("alg") != alg:
        return None
    kid = h.get("kid")
    if not isinstance(kid, str) or not kid or len(kid) > 256:
        return None
    return h


def _public_key(key: Any):
    """A cryptography EC public key from a JWK (dict or JSON text) or a key object."""
    import jwt  # PyJWT
    from cryptography.hazmat.primitives.asymmetric import ec

    if isinstance(key, ec.EllipticCurvePublicKey):
        return key
    if isinstance(key, (dict, str, bytes)):
        jwk = json.loads(key) if isinstance(key, (str, bytes)) else key
        if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or jwk.get("crv") not in (None, "P-256"):
            return None
        if jwk.get("alg") not in (None, "ES256"):
            return None
        pub = {k: jwk[k] for k in ("kty", "crv", "x", "y") if k in jwk}
        k = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(pub))
        return k if isinstance(k, ec.EllipticCurvePublicKey) else None
    return None


def jwt_es256(token: Any, get_key: Callable[[str], Any], *, body: bytes, now: float | None = None,
              tolerance_s: float = DEFAULT_TOLERANCE_S, body_hash_claim: str | None = "request_body_sha256",
              max_bytes: int = MAX_SIGNATURE_CHARS) -> bool:
    """An ES256 JWT (Plaid ``Plaid-Verification``).

    Order: structure and ``alg`` (offline, ``jwt_header``) → ``get_key(kid)`` (a JWK dict,
    JWK JSON or an EC public key; None means unknown) → signature → ``iat`` within
    ``tolerance_s`` of ``now`` (two-sided), ``exp``/``nbf`` against ``now`` when present →
    the ``body_hash_claim`` equals the hex SHA-256 of ``body``. The key lookup is the
    caller's; it is where caching and fetch limits live.
    """
    h = jwt_header(token, max_bytes=max_bytes)
    if h is None:
        return False
    try:
        key = get_key(h["kid"])
    except Exception:  # noqa: BLE001 - a failed lookup is "not verified"
        return False
    if key is None:
        return False
    import jwt  # PyJWT

    try:
        pub = _public_key(key)
        if pub is None:
            return False
        claims = jwt.decode(str(token).strip(), pub, algorithms=["ES256"],
                            options={"verify_exp": False, "verify_iat": False, "verify_nbf": False,
                                     "verify_aud": False, "verify_iss": False, "require": []})
    except (jwt.PyJWTError, ValueError, TypeError):
        return False
    now = time.time() if now is None else float(now)
    iat = claims.get("iat")
    if isinstance(iat, bool) or not isinstance(iat, (int, float)) or abs(now - float(iat)) > float(tolerance_s):
        return False
    for name, bad in (("exp", lambda v: now > float(v)), ("nbf", lambda v: now < float(v))):
        v = claims.get(name)
        if v is not None:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or bad(v):
                return False
    if body_hash_claim:
        claimed = claims.get(body_hash_claim)
        if not isinstance(claimed, str) or not equal(claimed.strip().lower(), sha256_hex(body)):
            return False
    return True


# ---- verification keys (JWKS) fetch and cache ------------------------------------------------------
KEY_TTL_S = 24 * 3600.0
KEY_NEGATIVE_TTL_S = 600.0
KEY_FETCH_COOLDOWN_S = 60.0
KEY_MAX_CACHED = 256
JWKS_MAX_KEYS = 64
JWKS_MAX_BYTES = 64 * 1024
JWKS_TIMEOUT_S = 5.0


class KeyCache:
    """A sender's published verification keys, by ``kid``, for a source's ``verify``.

    ``fetch(kid)`` asks the sender and returns one of: the JWK dict for ``kid`` (a per-key
    endpoint, such as Plaid's), a JWKS set ``{"keys": [...]}`` (every key in it is cached by its
    ``kid``; ``jwks_fetcher`` builds such a fetch for a URL), or None (the sender does not know
    ``kid``). It may raise; a failure is "no answer", never "unknown".

    Rules, shared by every source that uses it:

    - a known key is served for ``ttl_s``; an unknown ``kid`` is negative-cached for
      ``negative_ttl_s``;
    - a ``kid`` not in the cache (a rotation) or past its TTL triggers a fetch, but at most one
      fetch per ``cooldown_s`` for the whole cache, so requests with random ``kid`` s cannot
      amplify against the sender or tie up intake threads; the fetch runs outside the lock;
    - a known key past its TTL is still served while a refresh is blocked by the cooldown or
      fails, so a client holding the fetch slot cannot starve genuine requests; it is dropped
      only when a successful fetch says it is unknown (a JWKS set that no longer lists it), or
      ``usable(jwk)`` is false;
    - at most ``max_cached`` entries per table.

    ``now`` is the request clock (``req.now``)."""

    def __init__(self, fetch: Callable[[str], Any], *, ttl_s: float = KEY_TTL_S,
                 negative_ttl_s: float = KEY_NEGATIVE_TTL_S, cooldown_s: float = KEY_FETCH_COOLDOWN_S,
                 max_cached: int = KEY_MAX_CACHED, usable: Callable[[dict], bool] | None = None,
                 on_error: Callable[[BaseException], None] | None = None):
        self.fetch = fetch
        self.ttl_s = float(ttl_s)
        self.negative_ttl_s = float(negative_ttl_s)
        self.cooldown_s = float(cooldown_s)
        self.max_cached = int(max_cached)
        self.usable = usable or (lambda jwk: True)
        self.on_error = on_error
        self.lock = threading.Lock()
        self.keys: dict[str, tuple[float, dict]] = {}      # kid -> (expires, jwk)
        self.unknown: dict[str, float] = {}                # kid -> expires
        self.last_fetch_at = float("-inf")
        self.fetches = 0

    def _prune(self, now: float) -> None:
        for table in (self.keys, self.unknown):
            if len(table) > self.max_cached:
                for k in [k for k, e in table.items() if (e[0] if isinstance(e, tuple) else e) <= now]:
                    del table[k]
                while len(table) > self.max_cached:
                    del table[next(iter(table))]

    def _store(self, kid: str, jwk: Any, now: float) -> None:
        if isinstance(jwk, dict) and self.usable(jwk):
            self.keys[kid] = (now + self.ttl_s, jwk)
            self.unknown.pop(kid, None)
        else:
            self.unknown[kid] = now + self.negative_ttl_s
            self.keys.pop(kid, None)

    def lookup(self, kid: str, now: float | None = None) -> dict | None:
        """The JWK for ``kid``, or None."""
        now = time.time() if now is None else float(now)
        if not isinstance(kid, str) or not kid:
            return None
        with self.lock:
            hit = self.keys.get(kid)
            if hit is not None and hit[0] > now:
                return hit[1]
            stale = hit[1] if hit is not None else None          # past its TTL, still the best we know
            if self.unknown.get(kid, float("-inf")) > now:
                return None
            if now - self.last_fetch_at < self.cooldown_s and now >= self.last_fetch_at:
                return stale                                     # refresh blocked: keep serving the known key
            self.last_fetch_at = now
            self.fetches += 1
        try:
            got = self.fetch(kid)
        except Exception as e:  # noqa: BLE001 - a failed fetch keeps the known key; the sender retries
            if self.on_error is not None:
                self.on_error(e)
            return stale
        with self.lock:
            if isinstance(got, dict) and isinstance(got.get("keys"), list):
                for jwk in got["keys"][:JWKS_MAX_KEYS]:
                    k = jwk.get("kid") if isinstance(jwk, dict) else None
                    if isinstance(k, str) and k and len(k) <= 256:
                        self._store(k, jwk, now)
                listed = {jwk.get("kid") for jwk in got["keys"][:JWKS_MAX_KEYS] if isinstance(jwk, dict)}
                for k in [k for k in self.keys if k not in listed]:
                    del self.keys[k]                             # withdrawn from the set: revoked
                if kid not in self.keys:
                    self._store(kid, None, now)                  # not in the set: unknown
            else:
                self._store(kid, got, now)
            self._prune(now)
            hit = self.keys.get(kid)
        return hit[1] if hit is not None else None

    def reset(self) -> None:
        with self.lock:
            self.keys.clear()
            self.unknown.clear()
            self.last_fetch_at = float("-inf")


def jwks_fetcher(url: str, *, timeout_s: float = JWKS_TIMEOUT_S, max_bytes: int = JWKS_MAX_BYTES,
                 http: Any = None) -> Callable[[str], dict]:
    """A ``KeyCache`` fetch that GETs the JWKS set at ``url`` (through ``net.Http``, so
    errors carry no URL or body). The ``kid`` argument is ignored: the whole set is fetched."""
    from .net import Http

    client = http if http is not None else Http(timeout_s=timeout_s, max_response_bytes=max_bytes)

    def fetch(kid: str) -> dict:
        data = client.get(url, label="jwks")
        if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
            raise ValueError("not a JWKS document")
        return data

    return fetch


# ---- signing helpers (outbound deliveries, tests) --------------------------------------------------
def hmac_hex_sign(secret: str | bytes, message: str | bytes, algo: str = "sha256") -> str:
    return hmac_digest(secret, message, algo).hex()


def hmac_b64_sign(secret: str | bytes, message: str | bytes, algo: str = "sha256") -> str:
    return base64.b64encode(hmac_digest(secret, message, algo)).decode("ascii")
