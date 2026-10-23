"""Shared HTTP wire code for the ``hermes`` and ``http`` destinations (contract C3).

- Standard Webhooks signing (ARCHITECTURE §6.4): ``webhook-id``, ``webhook-timestamp`` and
  ``webhook-signature: v1,<b64>`` over ``<id>.<ts>.<body>``. The key is the secret's raw UTF-8
  bytes, or the base64-decoded remainder when the secret starts with ``whsec_``.
- ``post``: one HTTPS/HTTP POST that cannot outlive its budget. A watchdog thread bounds the
  whole exchange (DNS and connect included), the socket timeout bounds each blocking call, and
  the response is read in small pieces against the same deadline, so a receiver that drips a
  byte at a time cannot hold a worker past the lease deadline. Redirects are never followed.
- ``classify_status``: the §6.5 mapping from an HTTP status to an ``Outcome``.

Nothing here logs, and no message includes a secret, a header value or a body.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import http.client
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from .. import __version__
from .base import DeliveryContext, Outcome

USER_AGENT = f"hookspool/{__version__}"
READ_LIMIT = 65536                # response bytes kept; the rest is discarded with the connection
PERMANENT_STATUS = frozenset({400, 413, 415, 422})
CONFIG_STATUS = frozenset({401, 403, 404, 405, 410})
# Header names a destination's static ``headers`` table may never set: they are ours.
RESERVED_HEADERS = frozenset({
    "host", "content-length", "content-type", "user-agent", "connection", "transfer-encoding",
    "webhook-id", "webhook-timestamp", "webhook-signature", "x-hub-signature-256",
    "x-hookspool-batch", "x-hookspool-attempt",
})
_HEADER_VALUE = re.compile(r"[\x20-\x7e\xa0-\xff\t]*")


def header_value_ok(value: str) -> bool:
    """True when ``value`` can go on the wire as is: Latin-1 text with no control characters
    (http.client refuses anything else before sending, which would be a transport loop)."""
    return isinstance(value, str) and _HEADER_VALUE.fullmatch(value) is not None


class BadSecret(ValueError):
    """The secret cannot be used as a signing key. The message never contains the secret."""


# ---- signing ---------------------------------------------------------------------------------
def signing_key(secret: str) -> bytes:
    """``whsec_<base64>`` is decoded (an empty or undecodable remainder is refused); anything
    else is the raw UTF-8 bytes."""
    if secret.startswith("whsec_"):
        try:
            key = base64.b64decode(secret[len("whsec_"):], validate=True)
        except (binascii.Error, ValueError):
            raise BadSecret("whsec_ secret is not valid base64") from None
        if not key.strip(b"\x00 \t\r\n\x0b\x0c"):
            raise BadSecret("whsec_ secret decodes to an empty key")
        return key
    if not secret:
        raise BadSecret("secret is empty")
    return secret.encode("utf-8")


def sign(key: bytes, msg_id: str, timestamp: int, body: bytes) -> str:
    """base64(HMAC-SHA256(key, ``<id>.<ts>.<body>``))."""
    msg = msg_id.encode("utf-8") + b"." + str(int(timestamp)).encode("ascii") + b"." + body
    return base64.b64encode(hmac.new(key, msg, hashlib.sha256).digest()).decode("ascii")


def delivery_headers(secrets: list[str], delivery_id: str, ctx: DeliveryContext, body: bytes, *,
                     legacy: bool = False) -> dict[str, str]:
    """The headers of §6.4. ``secrets`` is the current secret first, then (rotation) the
    previous one; each adds a ``v1,<sig>`` entry. ``legacy`` adds ``X-Hub-Signature-256``
    from the current secret's raw bytes (what GitHub-style receivers compute)."""
    ts = int(ctx.now())
    sigs = " ".join("v1," + sign(signing_key(s), delivery_id, ts, body) for s in secrets)
    h = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "webhook-id": delivery_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": sigs,
        "X-Hookspool-Batch": str(ctx.batch_id),
        "X-Hookspool-Attempt": str(ctx.attempt),
    }
    if legacy:
        h["X-Hub-Signature-256"] = "sha256=" + hmac.new(secrets[0].encode("utf-8"), body,
                                                         hashlib.sha256).hexdigest()
    return h


# ---- transport -------------------------------------------------------------------------------
class NetError(Exception):
    """A request that did not produce a response. ``kind``: ``timeout`` | ``tls`` | ``unreachable``
    | ``protocol``; ``detail`` is the exception class name only."""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(kind)
        self.kind = kind
        self.detail = detail


@dataclass
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)   # lower-cased names
    body: bytes = b""


def tls_context() -> ssl.SSLContext:
    """Verifying client context (system roots; ``SSL_CERT_FILE``/``SSL_CERT_DIR`` apply)."""
    return ssl.create_default_context()


def post(url: str, headers: dict[str, str], body: bytes, budget_s: float, *,
         ssl_context: ssl.SSLContext | None = None) -> Response:
    """POST ``body`` to ``url`` and return the response, within ``budget_s`` seconds in total.

    Raises ``NetError``. Never follows a redirect and never uses environment proxies.
    """
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise NetError("protocol", "BadURL")
    if budget_s <= 0:
        raise NetError("timeout", "NoBudget")
    https = u.scheme == "https"
    port = u.port or (443 if https else 80)
    path = (u.path or "/") + (("?" + u.query) if u.query else "")
    end = time.monotonic() + budget_s
    box: dict = {}
    done = threading.Event()
    done_by_watchdog = threading.Event()

    def work() -> None:
        conn = None
        try:
            if https:
                conn = http.client.HTTPSConnection(u.hostname, port, timeout=budget_s,
                                                   context=ssl_context or tls_context())
            else:
                conn = http.client.HTTPConnection(u.hostname, port, timeout=budget_s)
            box["conn"] = conn
            hdrs = dict(headers)
            hdrs.setdefault("Connection", "close")
            conn.connect()      # DNS and the handshake; a late finisher must not send (overlaps the next attempt)
            if time.monotonic() >= end or done_by_watchdog.is_set():
                raise TimeoutError
            conn.request("POST", path, body=body, headers=hdrs)
            resp = conn.getresponse()
            got = bytearray()
            while len(got) < READ_LIMIT:
                left = end - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                if conn.sock is not None:
                    conn.sock.settimeout(left)
                chunk = resp.read1(min(4096, READ_LIMIT - len(got)))
                if not chunk:
                    break
                got += chunk
            box["resp"] = Response(resp.status, {k.lower(): v for k, v in resp.getheaders()}, bytes(got))
        except BaseException as e:      # reported to the caller thread
            box["err"] = e
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass
            done.set()

    t = threading.Thread(target=work, name="hookspool-post", daemon=True)
    t.start()
    if not done.wait(budget_s + 0.05):
        done_by_watchdog.set()
        conn = box.get("conn")      # unblock the thread; it exits with an error nobody reads
        sock = getattr(conn, "sock", None) if conn is not None else None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)     # wakes a thread blocked in send/recv (close alone may not)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass
        raise NetError("timeout", "Watchdog")
    err = box.get("err")
    if err is None:
        return box["resp"]
    name = err.__class__.__name__
    if isinstance(err, ssl.SSLCertVerificationError):
        raise NetError("tls", "SSLCertVerificationError") from None
    if isinstance(err, TimeoutError):
        raise NetError("timeout", name) from None
    if isinstance(err, ssl.SSLError):
        raise NetError("unreachable", name) from None
    if isinstance(err, http.client.HTTPException):
        raise NetError("protocol", name) from None
    if isinstance(err, OSError):
        raise NetError("unreachable", name) from None
    raise NetError("protocol", name) from None


def net_outcome(e: NetError, *, timed_out_reason: str) -> Outcome:
    """Outcome for a request that failed before a response. ``timed_out_reason`` is
    ``timeout`` or ``deadline`` depending on which bound applied."""
    if e.kind == "timeout":
        return Outcome.transport(timed_out_reason, detail=timed_out_reason)
    if e.kind == "tls":
        return Outcome.config("tls certificate verification failed", detail="tls certificate verification failed")
    if e.kind == "protocol":
        return Outcome.transport(f"protocol error ({e.detail})", detail=f"protocol error: {e.detail}")
    return Outcome.transport(f"unreachable ({e.detail})", detail=f"unreachable: {e.detail}")


# ---- status mapping --------------------------------------------------------------------------
def parse_retry_after(value: str | None, now: float) -> float | None:
    """Seconds from a ``Retry-After`` header (delta-seconds or HTTP-date), else None."""
    if not value:
        return None
    v = value.strip()
    try:
        s = float(v)
    except ValueError:
        try:
            s = parsedate_to_datetime(v).timestamp() - now
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    if s != s or s in (float("inf"), float("-inf")):
        return None
    return max(0.0, s)


def classify_status(resp: Response, now: float, *, redirect_permanent: bool) -> Outcome:
    """§6.5: the outcome for a non-2xx status (a 2xx is the caller's ``ok``)."""
    code = resp.status
    reason = f"http {code}"
    ra = parse_retry_after(resp.headers.get("retry-after"), now)
    if 200 <= code < 300:
        return Outcome.ok(reason, status=code)
    if 300 <= code < 400:
        if redirect_permanent:
            return Outcome.permanent(f"{reason} redirect (not followed)", status=code,
                                     detail=f"{reason}: redirects are never followed")
        return Outcome.config(f"{reason} redirect (not followed); fix the destination URL", status=code,
                              detail=f"{reason}: redirects are never followed")
    if code == 429:
        return Outcome.throttle(reason, status=code, retry_after_s=ra)
    if code == 503 and ra is not None:
        return Outcome.throttle(reason, status=code, retry_after_s=ra)
    if code in PERMANENT_STATUS:
        return Outcome.permanent(reason, status=code, detail=reason)
    if code in CONFIG_STATUS:
        return Outcome.config(reason, status=code, detail=reason)
    if code == 408 or code >= 500:
        return Outcome.transport(reason, status=code, detail=reason)
    if 400 <= code < 500:       # any other 4xx: the operator's to look at, never dropped
        return Outcome.config(reason, status=code, detail=reason)
    return Outcome.transport(reason, status=code, detail=reason)    # 1xx, >= 600: not a real answer
