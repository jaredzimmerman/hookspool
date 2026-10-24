"""Outbound HTTP, OAuth 2 refresh tokens and secret access for service processors (ARCHITECTURE 14.3.7).

A package never opens a socket or reads a credential itself; it calls this module through its
processor ``Context``:

- ``ctx.http`` is an ``Http``: JSON in and out, a timeout on every request, and every failure
  mapped to ``Retryable`` (the dispatcher retries the batch later) or ``Permanent`` (the batch is
  dead-lettered and needs a person). ``ctx.http.get_text`` is the conditional GET a poller needs
  (14.4): it returns ``(status, text, response_headers)`` and answers 304 without raising.
- ``ctx.pace(per_s)`` is a ``Pacer`` for a provider with a request-rate rule: call ``wait()`` before each request and
  they start at least ``1/per_s`` seconds apart. The sleep is core's, because a bobbin never sleeps (14.4).
- ``ctx.oauth(name)`` is an ``OAuthClient`` for the credential secret ``name``: it reads the
  credential through ``ctx.secret``, keeps the access token in the runtime's state mapping,
  refreshes it when it expires or after one 401, and never writes the credential file.
- ``ctx.secret(name)`` resolves a secret the manifest marks ``processor = true``.

``make_context_extras(state, secret_resolver)`` builds all of them; the processor runtime calls it
once per batch.

Status mapping (``Http``):

=====================================  ===============================================
2xx                                    the parsed JSON body (an empty body is ``{}``)
408, 425, 429, 5xx                     ``Retryable`` (``retry_after_s`` from Retry-After)
401                                    ``Unauthorized`` (a ``Permanent``)
any other 4xx                          ``Permanent``
3xx                                    ``Permanent``: redirects are never followed, so a
                                       bearer token never travels to another host
timeout, refused, reset, DNS           ``Retryable``
TLS certificate verification failed    ``Permanent`` (a configuration fault that needs a
                                       person, as the dispatcher's own outbound table has it)
2xx that is not JSON                   ``Retryable``
a response over ``max_response_bytes`` ``Permanent``
=====================================  ===============================================

Error text and log lines carry the method, the host, an optional caller label, the status and
the provider's short error code (letters and ``_`` only, at most 64 characters, for example
``invalid_grant`` or ``UNAUTHENTICATED``). They never carry a request or response body, a path,
a query string, a header value, a token or a secret, and they are raised with no chained
exception, so a traceback cannot show one either.

Plain ``http`` is refused except to a loopback address (``allow_insecure=True`` lifts that), and
so is every scheme other than ``http`` and ``https``.
"""
from __future__ import annotations

import email.utils
import hashlib
import http.client
import ipaddress
import json as _json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import MutableMapping
from typing import Any, Callable

from . import __version__
from .config import SecretError, resolve_secret

__all__ = ["NetError", "Retryable", "Permanent", "Unauthorized", "Http", "Pacer", "OAuth", "OAuthClient",
           "SecretAccess", "resolver_from_refs", "make_context_extras", "DEFAULT_TOKEN_URI"]

DEFAULT_TIMEOUT_S = 20.0
DEFAULT_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_ERROR_BYTES = 8192
MAX_RETRY_AFTER_S = 3600.0
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
TOKEN_SKEW_S = 60.0                  # refresh this long before the provider's expiry
DEFAULT_EXPIRES_IN_S = 3600.0
RETRYABLE_STATUS = frozenset({408, 425, 429})
_CODE_RE = re.compile(r"^[A-Za-z_]{1,64}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,64}$")
_XML_DECL_RE = re.compile(rb"""^\s*<\?xml[^>]{0,200}?encoding\s*=\s*["']([A-Za-z0-9_.:-]{1,40})["']""")
MAX_PACE_INTERVAL_S = 10.0
_READ_CHUNK = 65536


# ---- errors --------------------------------------------------------------------------------------
class NetError(Exception):
    """A failed call. The message is safe for logs (see the module docstring)."""

    def __init__(self, message: str, *, status: int | None = None, code: str = "",
                 retry_after_s: float | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after_s = retry_after_s


class Retryable(NetError):
    """Try the batch again later (the dispatcher's backoff; never loop or sleep in a package)."""


class Permanent(NetError):
    """Retrying cannot help; the batch is dead-lettered for a person to look at."""


class Unauthorized(Permanent):
    """HTTP 401. ``OAuthClient`` refreshes once on it before giving up."""


# ---- helpers -------------------------------------------------------------------------------------
def _safe_code(raw: bytes) -> str:
    """The provider's short error code from a JSON error body, or "". Only a token of letters and
    ``_`` survives, so nothing else from the body can reach an exception or a log line."""
    try:
        data = _json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    cands = [data.get("error"), data.get("error_code"), data.get("code")]
    err = data.get("error")
    if isinstance(err, dict):
        cands = [err.get("status"), err.get("code"), err.get("error_code")]
    for c in cands:
        if isinstance(c, str) and _CODE_RE.match(c):
            return c
    return ""


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        return min(float(value), MAX_RETRY_AFTER_S)
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    return max(0.0, min(dt.timestamp() - time.time(), MAX_RETRY_AFTER_S))


def _lower_headers(headers: Any) -> dict[str, str]:
    """Response headers as a plain dict with lower-case names (the last value of a repeated header wins)."""
    out: dict[str, str] = {}
    if headers is None:
        return out
    try:
        items = headers.items()
    except AttributeError:
        return out
    for k, v in items:
        out[str(k).lower()] = str(v)
    return out


def _charset(content_type: str) -> str:
    """The ``charset`` of a Content-Type value if Python knows it, else UTF-8."""
    import codecs
    m = re.search(r"(?i)charset\s*=\s*\"?([A-Za-z0-9_.:-]+)", content_type or "")
    if m:
        try:
            return codecs.lookup(m.group(1)).name
        except LookupError:
            pass
    return "utf-8"


def _body_charset(content_type: str, raw: bytes) -> str:
    """The charset of a text body: the Content-Type's, else an XML declaration's ``encoding=``, else UTF-8."""
    import codecs
    if re.search(r"(?i)charset\s*=", content_type or ""):
        return _charset(content_type)
    m = _XML_DECL_RE.match(raw[:300])
    if m:
        try:
            return codecs.lookup(m.group(1).decode("ascii")).name
        except LookupError:
            pass
    return "utf-8"


def _is_loopback(host: str) -> bool:
    if host.lower() in ("localhost", "localhost."):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102 - never follow
        return None


def classify_status(status: int) -> type[NetError] | None:
    """The error class for a non-2xx status (None for 2xx)."""
    if 200 <= status < 300:
        return None
    if status == 401:
        return Unauthorized
    if status in RETRYABLE_STATUS or status >= 500:
        return Retryable
    return Permanent


# ---- Http ----------------------------------------------------------------------------------------
class Http:
    """JSON over HTTPS with a timeout on every request and errors mapped to ``Retryable`` /
    ``Permanent``.

    ``timeout_s`` bounds the whole request: each socket operation gets what is left of it, and
    the body is read in chunks against the same deadline, so a server that drips its response
    cannot hold a processor past it (name resolution is the operating system's and is not
    covered)."""

    def __init__(self, *, timeout_s: float = DEFAULT_TIMEOUT_S,
                 max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES, allow_insecure: bool = False,
                 user_agent: str | None = None, clock: Callable[[], float] = time.monotonic):
        self.timeout_s = float(timeout_s)
        self.max_response_bytes = int(max_response_bytes)
        self.allow_insecure = bool(allow_insecure)
        self.user_agent = user_agent or f"hookspool/{__version__}"
        self._clock = clock
        # proxies come from the environment only (HTTPS_PROXY, HTTP_PROXY, NO_PROXY), never from the
        # platform's own settings: a deployment sets them for a processor through its env table (14.3.3)
        self._opener = urllib.request.build_opener(
            _NoRedirect, urllib.request.ProxyHandler(urllib.request.getproxies_environment()))

    # public API ---------------------------------------------------------------------------------
    def get(self, url: str, *, params: dict | None = None, headers: dict | None = None,
            bearer: str | None = None, timeout_s: float | None = None, label: str | None = None) -> Any:
        return self.request("GET", url, params=params, headers=headers, bearer=bearer, timeout_s=timeout_s,
                            label=label)

    def post(self, url: str, *, json: Any = None, form: dict | None = None, params: dict | None = None,
             headers: dict | None = None, bearer: str | None = None, timeout_s: float | None = None,
             label: str | None = None) -> Any:
        return self.request("POST", url, json=json, form=form, params=params, headers=headers, bearer=bearer,
                            timeout_s=timeout_s, label=label)

    def get_text(self, url: str, headers: dict | None = None, *, params: dict | None = None,
                 bearer: str | None = None, timeout_s: float | None = None,
                 label: str | None = None) -> tuple[int, str, dict[str, str]]:
        """A GET that returns ``(status, text, response_headers)`` instead of parsed JSON, for a feed, a page or
        an API that answers a conditional request (14.4).

        Send ``If-None-Match`` (the ``etag`` header of the last answer) or ``If-Modified-Since`` in ``headers``:
        a 304 comes back as ``(304, "", headers)`` and is not an error. A 2xx body is decoded with the charset of
        its ``Content-Type`` (else the encoding in an XML declaration, else UTF-8; undecodable bytes become U+FFFD).
        Header names are lower case. Every other status, the timeout and the size cap are those of ``request``."""
        prefix = self._target("GET", url, label)
        if params:
            url = url + ("&" if urllib.parse.urlsplit(url).query else "?") + urllib.parse.urlencode(params,
                                                                                                    doseq=True)
        hdrs = {"Accept": "*/*", "User-Agent": self.user_agent}
        for k, v in (headers or {}).items():
            hdrs[str(k)] = str(v)
        if bearer is not None:
            hdrs["Authorization"] = f"Bearer {bearer}"
        budget = self.timeout_s if timeout_s is None else float(timeout_s)
        deadline = self._clock() + max(budget, 0.001)
        meta: dict[str, Any] = {}
        err, raw = self._send("GET", url, None, hdrs, deadline, prefix, meta=meta)
        if err is not None:
            raise err
        rh = meta.get("headers") or {}
        status = int(meta.get("status") or 200)
        if status == 304:
            return 304, "", rh
        return status, raw.decode(_body_charset(rh.get("content-type", ""), raw), errors="replace"), rh

    def request(self, method: str, url: str, *, json: Any = None, form: dict | None = None,
                params: dict | None = None, headers: dict | None = None, bearer: str | None = None,
                timeout_s: float | None = None, label: str | None = None) -> Any:
        """Send one request and return the parsed JSON body (``{}`` when the body is empty).

        ``json`` sends a JSON body, ``form`` a form-encoded one (not both); ``params`` is added
        to the query string; ``bearer`` is sent as ``Authorization: Bearer``. ``label`` (letters,
        digits and ``_.:/-``) names the call in error text, since the path never appears there."""
        method = str(method).upper()
        prefix = self._target(method, url, label)
        if json is not None and form is not None:
            raise Permanent(f"{prefix}: json and form are exclusive")
        if params:
            url = url + ("&" if urllib.parse.urlsplit(url).query else "?") + urllib.parse.urlencode(params,
                                                                                                    doseq=True)
        hdrs = {"Accept": "application/json", "User-Agent": self.user_agent}
        data = None
        if json is not None:
            data = _json.dumps(json, separators=(",", ":")).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode("ascii")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        elif method in ("POST", "PUT", "PATCH"):
            data = b""
        for k, v in (headers or {}).items():
            hdrs[str(k)] = str(v)
        if bearer is not None:
            hdrs["Authorization"] = f"Bearer {bearer}"
        budget = self.timeout_s if timeout_s is None else float(timeout_s)
        deadline = self._clock() + max(budget, 0.001)
        err, raw = self._send(method, url, data, hdrs, deadline, prefix)
        if err is not None:
            raise err
        if not raw.strip():
            return {}
        try:
            return _json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            pass
        raise Retryable(f"{prefix}: response is not JSON")

    # internals ----------------------------------------------------------------------------------
    def _target(self, method: str, url: str, label: str | None) -> str:
        tag = f" {label}" if isinstance(label, str) and _LABEL_RE.match(label) else ""
        try:
            parts = urllib.parse.urlsplit(str(url))
            host = parts.hostname or ""
        except ValueError:
            raise Permanent(f"{method}{tag}: invalid URL") from None
        prefix = f"{method} {host or '?'}{tag}"
        if parts.scheme not in ("http", "https") or not host:
            raise Permanent(f"{prefix}: only http and https URLs with a host are allowed")
        if parts.scheme == "http" and not (self.allow_insecure or _is_loopback(host)):
            raise Permanent(f"{prefix}: plain http is allowed only to a loopback address")
        return prefix

    def _left(self, deadline: float) -> float:
        return deadline - self._clock()

    def _send(self, method, url, data, hdrs, deadline, prefix, meta: dict | None = None) -> tuple[NetError | None, bytes]:
        """(error, body). The error is built here and raised by the caller, outside every except
        block, so it carries no chained exception (whose text could hold a URL). With ``meta`` (``get_text``),
        the status and the response headers are put in it, and a 304 is an answer, not an error."""
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        except ValueError:
            return Permanent(f"{prefix}: invalid request"), b""
        resp = None
        try:
            left = self._left(deadline)
            if left <= 0:
                return Retryable(f"{prefix}: timed out"), b""
            try:
                resp = self._opener.open(req, timeout=left)
            except urllib.error.HTTPError as e:
                if meta is not None and int(e.code) == 304:
                    meta.update(status=304, headers=_lower_headers(e.headers))
                    try:
                        e.close()
                    except Exception:  # noqa: BLE001,S110 - closing an answered request
                        pass
                    return None, b""
                return self._http_error(e, prefix, deadline), b""
            if meta is not None:
                meta.update(status=getattr(resp, "status", 200) or 200,
                            headers=_lower_headers(getattr(resp, "headers", None)))
            return self._read(resp, deadline, prefix)
        except (TimeoutError, ConnectionError) as e:
            return Retryable(f"{prefix}: {'timed out' if isinstance(e, TimeoutError) else e.__class__.__name__}"), b""
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                return Permanent(f"{prefix}: TLS certificate verification failed"), b""
            what = "timed out" if isinstance(reason, TimeoutError) else (
                reason.__class__.__name__ if isinstance(reason, BaseException) else "connection failed")
            return Retryable(f"{prefix}: {what}"), b""
        except (http.client.HTTPException, OSError) as e:
            return Retryable(f"{prefix}: {e.__class__.__name__}"), b""
        except ValueError:
            return Permanent(f"{prefix}: invalid request"), b""
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:  # noqa: BLE001,S110 - closing a failed response
                    pass

    def _http_error(self, e: urllib.error.HTTPError, prefix: str, deadline: float) -> NetError:
        status = int(e.code)
        chunks, total = [], 0
        try:
            while total < MAX_ERROR_BYTES:           # bounded by the request's own deadline, like _read
                left = self._left(deadline)
                if left <= 0:
                    break
                self._set_socket_timeout(e, left)
                b = e.read1(MAX_ERROR_BYTES - total) if hasattr(e, "read1") else e.read(MAX_ERROR_BYTES - total)
                if not b:
                    break
                chunks.append(b)
                total += len(b)
        except Exception:  # noqa: BLE001 - an unreadable error body just has no code
            pass
        raw = b"".join(chunks)
        try:
            e.close()
        except Exception:  # noqa: BLE001,S110
            pass
        code = _safe_code(raw)
        cls = classify_status(status) or Permanent
        if 300 <= status < 400:
            cls = Permanent
        retry_after = _retry_after(e.headers.get("Retry-After") if e.headers is not None else None)
        msg = f"{prefix}: HTTP {status}" + (f" {code}" if code else "")
        if 300 <= status < 400:
            msg += " (redirects are not followed)"
        return cls(msg, status=status, code=code, retry_after_s=retry_after if cls is Retryable else None)

    @staticmethod
    def _set_socket_timeout(resp, left: float) -> None:
        sock, obj = None, resp
        for _ in range(3):       # a response, or an HTTPError wrapping one
            sock = getattr(getattr(getattr(obj, "fp", None), "raw", None), "_sock", None)
            if sock is not None:
                break
            obj = getattr(obj, "fp", None)
        if sock is not None:
            try:
                sock.settimeout(left)
            except OSError:
                pass

    def _read(self, resp, deadline, prefix) -> tuple[NetError | None, bytes]:
        status = getattr(resp, "status", 200) or 200
        if not 200 <= status < 300:                 # a 1xx/3xx that urllib handed back as a response
            return Permanent(f"{prefix}: HTTP {status}", status=status), b""
        cap = self.max_response_bytes
        chunks, total = [], 0
        while True:
            left = self._left(deadline)
            if left <= 0:
                return Retryable(f"{prefix}: timed out reading the response"), b""
            self._set_socket_timeout(resp, left)
            b = resp.read1(min(_READ_CHUNK, cap + 1 - total)) if hasattr(resp, "read1") else resp.read(
                min(_READ_CHUNK, cap + 1 - total))
            if not b:
                break
            chunks.append(b)
            total += len(b)
            if total > cap:
                return Permanent(f"{prefix}: response larger than {cap} bytes", status=status), b""
        return None, b"".join(chunks)


# ---- secrets -------------------------------------------------------------------------------------
class SecretAccess:
    """``ctx.secret(name)``: the secret's value, read now. An unknown name is ``Permanent`` (the
    package asked for a secret its manifest does not give the processor); a reference that
    cannot be read now is ``Retryable``. Neither message contains a value (a ``SecretError`` text
    may name the variable or file that could not be read, never its content)."""

    def __init__(self, resolver: Callable[[str], str]):
        self._resolver = resolver

    def __call__(self, name: str) -> str:
        err: NetError | None = None
        try:
            value = self._resolver(name)
        except KeyError:
            err = Permanent(f"secret {name!s}: not available to this processor")
        except SecretError as e:
            err = Retryable(f"secret {name!s}: {e}")
        except NetError as e:
            err = e
        except Exception as e:  # noqa: BLE001 - a custom resolver's failure, class only
            err = Retryable(f"secret {name!s}: {e.__class__.__name__}")
        if err is not None:
            raise err
        if not isinstance(value, str) or not value:
            raise Retryable(f"secret {name!s}: empty")
        return value

    def __repr__(self) -> str:
        return "<SecretAccess>"


def resolver_from_refs(refs: dict) -> Callable[[str], str]:
    """A resolver over the processor's secret references (secret name -> reference, 14.3.3), reading each
    reference through ``config.resolve_secret`` at call time, so a rotated file is picked up."""
    refs = dict(refs or {})

    def resolve(name: str) -> str:
        if name not in refs:
            raise KeyError(name)
        return resolve_secret(refs[name])

    return resolve


# ---- OAuth 2 refresh tokens ------------------------------------------------------------------------
def _fingerprint(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(p.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:24]


class OAuth:
    """``ctx.oauth``: ``oauth(name)`` returns the ``OAuthClient`` for credential secret ``name``.

    The credential is JSON ``{client_id, client_secret, refresh_token, token_uri?}`` (other keys
    are ignored; ``token_uri`` defaults to ``DEFAULT_TOKEN_URI``). Per credential, the
    caller-supplied ``state`` mapping holds, under ``state["oauth"][name]``: the access token,
    its expiry, a rotated refresh token when the provider issued one, and a fingerprint of the
    credential it came from. When the credential's refresh token or client id changes (someone
    re-authorised), the entry is dropped and the new credential is used.

    The credential file is never written. A rotated refresh token lives only in ``state`` (and
    whatever the runtime does with it), so it must reach disk before anything can kill the process:
    ``persist`` is called, with no arguments, right after a rotation is recorded in ``state`` and
    before the access token is used, so the runtime can write its state file atomically at that
    moment rather than after the batch (a worker killed at its deadline, or a crash, between the
    rotation and the end of the batch would otherwise lose the new token while the provider may
    already have invalidated the old one). A failing ``persist`` is logged (its class only) and
    does not fail the request: ``changed`` stays set for the runtime's final save.

    The mapping then holds bearer tokens and a refresh token in the clear: the directory the runtime
    keeps it in must be private (the processor runtime writes it 0600 inside a 0700 directory; an
    operator-set state directory needs the same)."""

    def __init__(self, http: Http, secret: Callable[[str], str], state: MutableMapping, *,
                 now: Callable[[], float] = time.time, log: Callable[[str], None] | None = None,
                 skew_s: float = TOKEN_SKEW_S, persist: Callable[[], None] | None = None):
        self.http = http
        self.secret = secret
        self.state = state
        self.now = now
        self.log = log or (lambda line: None)
        self.skew_s = float(skew_s)
        self.persist = persist
        self.changed = False
        self._clients: dict[str, OAuthClient] = {}

    def __call__(self, name: str) -> OAuthClient:
        c = self._clients.get(name)
        if c is None:
            c = self._clients[name] = OAuthClient(self, name)
        return c

    def __repr__(self) -> str:
        return "<OAuth>"

    # state ----------------------------------------------------------------------------------------
    def _table(self) -> MutableMapping:
        t = self.state.get("oauth")
        if not isinstance(t, MutableMapping):
            t = self.state["oauth"] = {}
            self.changed = True
        return t

    def _entry(self, name: str, base: str) -> dict:
        t = self._table()
        e = t.get(name)
        if not isinstance(e, dict) or e.get("base") != base:
            if e is not None:
                self.log(f"oauth {name}: credential changed; cached token dropped")
            e = t[name] = {"base": base}
            self.changed = True
        return e

    def _credential(self, name: str) -> dict:
        raw = self.secret(name)
        try:
            cred = _json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            cred = None
        if not isinstance(cred, dict):
            raise Permanent(f"oauth {name}: credential is not a JSON object")
        missing = [k for k in ("client_id", "client_secret", "refresh_token")
                   if not isinstance(cred.get(k), str) or not cred.get(k)]
        if missing:
            raise Permanent(f"oauth {name}: credential lacks {', '.join(missing)}")
        uri = cred.get("token_uri") or DEFAULT_TOKEN_URI
        if not isinstance(uri, str):
            raise Permanent(f"oauth {name}: token_uri is not a string")
        return {"client_id": cred["client_id"], "client_secret": cred["client_secret"],
                "refresh_token": cred["refresh_token"], "token_uri": uri}

    # tokens ---------------------------------------------------------------------------------------
    def token(self, name: str, *, force: bool = False) -> str:
        cred = self._credential(name)
        e = self._entry(name, _fingerprint(cred["client_id"], cred["refresh_token"]))
        now = float(self.now())
        tok, exp = e.get("access_token"), e.get("expires_at")
        if not force and isinstance(tok, str) and tok and isinstance(exp, (int, float)) and now < exp - self.skew_s:
            return tok
        rotated = e.get("refresh_token")
        if isinstance(rotated, str) and rotated:
            try:
                return self._refresh(name, cred, e, rotated, now)
            except Permanent as err:
                if err.status not in (400, 401):
                    raise
                # the rotated token was refused (lost rotation, revoked): the credential's own
                # token is the only other one there is; try it once
                self.log(f"oauth {name}: rotated refresh token refused; trying the credential's own")
                e.pop("refresh_token", None)
                self.changed = True
        return self._refresh(name, cred, e, cred["refresh_token"], now)

    def invalidate(self, name: str) -> None:
        e = self._table().get(name)
        if isinstance(e, dict) and "access_token" in e:
            e.pop("access_token", None)
            e.pop("expires_at", None)
            self.changed = True

    def _refresh(self, name: str, cred: dict, e: dict, refresh_token: str, now: float) -> str:
        form = {"grant_type": "refresh_token", "client_id": cred["client_id"],
                "client_secret": cred["client_secret"], "refresh_token": refresh_token}
        err: NetError | None = None
        try:
            tok = self.http.post(cred["token_uri"], form=form, label="oauth.token")
        except Permanent as x:          # includes a 401 from the token endpoint (invalid_client)
            err = Permanent(f"oauth {name}: token refresh refused: {_tail(x)}", status=x.status, code=x.code)
        except Retryable as x:
            err = Retryable(f"oauth {name}: token refresh failed: {_tail(x)}", status=x.status, code=x.code,
                            retry_after_s=x.retry_after_s)
        if err is not None:
            raise err
        access = tok.get("access_token") if isinstance(tok, dict) else None
        if not isinstance(access, str) or not access:
            raise Retryable(f"oauth {name}: token response has no access_token")
        try:
            expires_in = float(tok.get("expires_in", DEFAULT_EXPIRES_IN_S))
        except (TypeError, ValueError):
            expires_in = DEFAULT_EXPIRES_IN_S
        e["access_token"] = access
        e["expires_at"] = now + max(expires_in, 0.0)
        new_rt = tok.get("refresh_token")
        rotated = isinstance(new_rt, str) and new_rt and new_rt != refresh_token
        if rotated:
            e["refresh_token"] = new_rt
        self.changed = True
        self.log(f"oauth {name}: access token refreshed (expires in {int(expires_in)}s)"
                 + ("; rotated refresh token kept in state" if rotated else ""))
        if rotated and self.persist is not None:
            try:
                self.persist()
            except Exception as x:  # noqa: BLE001 - the batch goes on; the runtime saves again at the end
                self.log(f"oauth {name}: persisting the rotated refresh token failed ({x.__class__.__name__})")
        return access


def _tail(err: NetError) -> str:
    """The part of an Http error after its ``METHOD host label:`` prefix."""
    s = str(err)
    return s.split(": ", 1)[1] if ": " in s else s


class OAuthClient:
    """Requests with the credential's bearer token. A 401 refreshes the token once and retries;
    a second 401 is ``Unauthorized`` (``Permanent``)."""

    def __init__(self, oauth: OAuth, name: str):
        self._oauth = oauth
        self.name = name

    def __repr__(self) -> str:
        return f"<OAuthClient {self.name}>"

    def token(self) -> str:
        return self._oauth.token(self.name)

    def request(self, method: str, url: str, **kw) -> Any:
        kw.pop("bearer", None)
        http = self._oauth.http
        try:
            return http.request(method, url, bearer=self.token(), **kw)
        except Unauthorized:
            pass
        self._oauth.invalidate(self.name)
        tok = self._oauth.token(self.name, force=True)
        return http.request(method, url, bearer=tok, **kw)

    def get(self, url: str, **kw) -> Any:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> Any:
        return self.request("POST", url, **kw)

    def get_text(self, url: str, headers: dict | None = None, **kw) -> tuple[int, str, dict[str, str]]:
        """``Http.get_text`` with the credential's bearer token (one refresh on a 401, as ``request``)."""
        kw.pop("bearer", None)
        http = self._oauth.http
        try:
            return http.get_text(url, headers, bearer=self.token(), **kw)
        except Unauthorized:
            pass
        self._oauth.invalidate(self.name)
        tok = self._oauth.token(self.name, force=True)
        return http.get_text(url, headers, bearer=tok, **kw)


# ---- Pacer ---------------------------------------------------------------------------------------
class Pacer:
    """Keeps requests under a provider's rate rule (for example "at most 10 a second").

    ``wait()`` before each request: the first returns at once, each later one starts at least
    ``1/per_s`` seconds after the previous. ``clock`` and ``sleep`` are injectable so a test never
    waits. One ``Pacer`` covers one poll in one process; a deployment that runs several instances
    against one provider from one address splits the budget between them."""

    def __init__(self, per_s: float, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        if isinstance(per_s, bool) or not isinstance(per_s, (int, float)) or per_s <= 0 or per_s != per_s:
            raise Permanent("pace: requests per second must be a number greater than 0")
        self.interval_s = 1.0 / float(per_s)
        if self.interval_s > MAX_PACE_INTERVAL_S:
            raise Permanent(f"pace: at least {1.0 / MAX_PACE_INTERVAL_S:g} request(s) per second")
        self._clock = clock
        self._sleep = sleep
        self._next: float | None = None

    def wait(self) -> float:
        """Block until the next request may start; returns how long it waited (seconds)."""
        t = self._clock()
        start = t if self._next is None else max(t, self._next)
        delay = start - t
        if delay > 0:
            self._sleep(delay)
        self._next = start + self.interval_s
        return max(delay, 0.0)


# ---- the processor runtime's entry point ----------------------------------------------------------
def make_context_extras(state: MutableMapping, secret_resolver: Callable[[str], str], *,
                        log: Callable[[str], None] | None = None, now: Callable[[], float] = time.time,
                        http: Http | None = None, persist: Callable[[], None] | None = None,
                        sleep: Callable[[float], None] = time.sleep) -> dict:
    """``{"http", "oauth", "secret", "pace"}`` for a processor ``Context``.

    ``pace(per_s)`` builds a ``Pacer`` that sleeps with ``sleep`` (the conformance kit and the tests pass one that
    does not wait).

    ``state`` is a mutable mapping the runtime owns and persists (OAuth keeps its tokens under
    ``state["oauth"]``; see ``OAuth``). ``secret_resolver(name)`` returns a secret's value or
    raises ``KeyError`` for a name the processor may not use (``resolver_from_refs`` builds one
    from the secret references the runtime is given, 14.3.3). ``log`` receives safe one-line messages.

    ``persist()`` is optional and is called right after a rotated refresh token is recorded in
    ``state`` (see ``OAuth``); the runtime passes its atomic state writer so the token survives a
    kill or crash before the batch ends.

    The ``KeyError`` convention is the resolver's: ``resolver_from_refs`` raises it for a name the
    processor was not given, which becomes ``Permanent``. A resolver that raises ``SecretError`` for
    an undeclared name instead (as the plugin ``Secrets`` mapping does) gets ``Retryable``, so a
    runtime that wants the permanent class must wrap its mapping to raise ``KeyError`` for that case."""
    secret = SecretAccess(secret_resolver)
    h = http if http is not None else Http()
    return {"http": h, "oauth": OAuth(h, secret, state, now=now, log=log, persist=persist), "secret": secret,
            "pace": lambda per_s: Pacer(per_s, sleep=sleep)}
