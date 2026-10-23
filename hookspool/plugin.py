"""Source plugin API (contract C4; ARCHITECTURE §14.1).

A source is a module that defines ``verify``, ``key`` and ``auth_headers`` (mandatory) and
optionally ``name``, ``secrets``, ``keep_headers``, ``span``, ``expect_every_s``,
``sender_retries``, ``challenge_unauthenticated``, ``config_keys``, ``challenge``,
``ignore``, ``payload``, ``dedup_id``, ``event_type``, ``attrs`` and ``action``::

    from hookspool.plugin import Request, Response, Secrets, verify as v, dedup

    auth_headers = ("X-Hub-Signature-256",)
    secrets = ("secret",)
    action = "hermes:github"

    def verify(req: Request, secrets: Secrets) -> bool:
        return v.hmac_prefixed(req.header("X-Hub-Signature-256"), "sha256=", secrets["secret"], req.body)

    def key(payload: dict, req: Request) -> str:
        return payload["repository"]["full_name"]

    dedup_id = dedup.header("X-GitHub-Delivery")

The intake wraps the module in ``Source``, which supplies the defaults and is the only
thing the intake, ``hookspool source test`` and the test kit call.

This module also defines the ``.http`` capture format used by ``hookspool source test``
and ``tests/captures/<source>/*.http``: optional ``# name: value`` metadata lines, the
request line, header lines, a blank line, then the body (exactly ``Content-Length`` bytes
when that header is present, otherwise the rest of the file minus one trailing newline).
Metadata keys: ``now`` (unix seconds the capture was signed at) and ``expect`` (the
status the kit expects: ``queued``, ``ignored``, ``challenge`` or ``unverified``).
"""
from __future__ import annotations

import hashlib
import json
import types
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator
from urllib.parse import parse_qsl, urlsplit

from . import config as _config
from . import verify  # noqa: F401 - re-exported for plugins: ``from hookspool.plugin import verify as v``
from .config import SecretError

__all__ = ["Request", "Response", "Secrets", "NoSecrets", "SecretError", "SecretAccessDenied",
           "BadPayload", "PluginError", "Source", "load_source", "dedup", "get_path", "verify",
           "RawRequest", "parse_capture", "format_capture", "ALWAYS_STRIPPED", "ROUTE_HEADER"]

ROUTE_HEADER = "X-Hookspool-Route"
# Never stored, whatever a source declares: generic credential headers.
ALWAYS_STRIPPED = ("Authorization", "Proxy-Authorization", "Cookie")


class BadPayload(ValueError):
    """The body is not the JSON object a plugin expected."""


class PluginError(Exception):
    """A plugin module is missing a mandatory name or defines one with the wrong type."""


class SecretAccessDenied(LookupError):
    """A challenge that runs before verification tried to read a secret."""


# ---- request / response ---------------------------------------------------------------------------
class Request:
    """One inbound request as plugins see it. Headers are case-insensitive; ``header``
    returns the first value or ``""``."""

    __slots__ = ("method", "path", "query", "headers", "body", "route_hint", "remote_addr", "now",
                 "config", "_json", "_index")

    def __init__(self, method: str, path: str, headers: list[tuple[str, str]] | Mapping[str, str] | None = None,
                 body: bytes = b"", *, query: Mapping[str, str] | None = None, remote_addr: str = "",
                 now: float = 0.0, config: Mapping[str, Any] | None = None):
        self.method = method.upper()
        parts = urlsplit(path)
        self.path = parts.path or "/"
        q = dict(parse_qsl(parts.query, keep_blank_values=True)) if parts.query else {}
        if query:
            q.update(query)
        self.query = q
        if headers is None:
            headers = []
        elif isinstance(headers, Mapping):
            headers = list(headers.items())
        self.headers = [(str(k), str(v)) for k, v in headers]
        idx: dict[str, str] = {}
        for k, v in self.headers:
            idx.setdefault(k.lower(), v)
        self._index = idx
        self.body = bytes(body or b"")
        self.route_hint = idx.get(ROUTE_HEADER.lower(), "")
        self.remote_addr = remote_addr
        self.now = float(now)
        self.config = types.MappingProxyType(dict(config or {}))
        self._json: Any = _UNSET

    def header(self, name: str) -> str:
        return self._index.get(name.lower(), "")

    def json(self) -> Any:
        """The body parsed as JSON (cached). Raises BadPayload."""
        if self._json is _UNSET:
            try:
                self._json = json.loads(self.body.decode("utf-8")) if self.body else None
            except (UnicodeDecodeError, ValueError):
                raise BadPayload("body is not JSON") from None
        return self._json

    def __repr__(self) -> str:  # never shows headers, query or body
        return f"<Request {self.method} {self.path} {len(self.body)} bytes>"


class _Unset:
    pass


_UNSET = _Unset()


@dataclass
class Response:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, status: int, obj: Any, headers: dict[str, str] | None = None) -> "Response":
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        return cls(status, json.dumps(obj, separators=(",", ":")).encode("utf-8"), h)

    def key(self) -> tuple:
        """Comparable form (the kit uses it to prove a handshake answer is constant)."""
        return (self.status, bytes(self.body), tuple(sorted((k.lower(), v) for k, v in self.headers.items())))


# ---- secrets --------------------------------------------------------------------------------------------
class Secrets(Mapping):
    """Lazy mapping of secret name -> value. Each access resolves the reference now (no
    caching, so file refs rotate without a restart). Missing or unreadable refs raise
    ``SecretError``, whose message never contains a value."""

    def __init__(self, refs: Mapping[str, Any] | None, source: str = ""):
        self._refs = dict(refs or {})
        self._source = source

    def __getitem__(self, name: str) -> str:
        if name not in self._refs:
            raise SecretError(f"source {self._source}: no secret {name!r} configured")
        return _config.resolve_secret(self._refs[name])

    def __iter__(self) -> Iterator[str]:
        return iter(self._refs)

    def __len__(self) -> int:
        return len(self._refs)

    def __repr__(self) -> str:
        return f"<Secrets {sorted(self._refs)}>"


class NoSecrets(Mapping):
    """What a ``challenge_unauthenticated`` challenge receives: every access raises."""

    def __getitem__(self, name: str) -> str:
        raise SecretAccessDenied("an unauthenticated challenge may not read secrets")

    def __iter__(self) -> Iterator[str]:
        raise SecretAccessDenied("an unauthenticated challenge may not read secrets")

    def __len__(self) -> int:
        raise SecretAccessDenied("an unauthenticated challenge may not read secrets")

    def __contains__(self, name: object) -> bool:
        raise SecretAccessDenied("an unauthenticated challenge may not read secrets")


# ---- helpers for plugins ----------------------------------------------------------------------------------
def get_path(obj: Any, dotted: str, default: Any = None) -> Any:
    """``a.b.0.c`` into nested dicts/lists; ``default`` when any step is missing."""
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return default
    return cur


class _Dedup:
    """Built-in dedup id functions: ``dedup_id = dedup.header("X-GitHub-Delivery")``."""

    @staticmethod
    def body_sha256(payload: Any, req: Request) -> str:
        return hashlib.sha256(req.body).hexdigest()

    @staticmethod
    def header(name: str) -> Callable[[Any, Request], str | None]:
        def f(payload: Any, req: Request) -> str | None:
            return req.header(name) or None
        f.__name__ = f"dedup_header_{name}"
        return f

    @staticmethod
    def field(dotted: str) -> Callable[[Any, Request], str | None]:
        def f(payload: Any, req: Request) -> str | None:
            v = get_path(payload, dotted)
            if v is None or isinstance(v, (dict, list)) or v == "":
                return None
            return str(v)
        f.__name__ = f"dedup_field_{dotted}"
        return f


dedup = _Dedup()


# ---- the wrapper the intake calls --------------------------------------------------------------------------
def _names(v: Any, what: str) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        return (v,)
    try:
        out = tuple(v)
    except TypeError:
        raise PluginError(f"{what} must be a sequence of strings") from None
    if not all(isinstance(x, str) and x for x in out):
        raise PluginError(f"{what} must be a sequence of non-empty strings")
    return out


class Source:
    """A loaded plugin bound to its config table, with every optional name defaulted."""

    def __init__(self, name: str, module: Any, source_cfg: Mapping[str, Any] | None = None):
        self.name = name
        self.module = module
        self.cfg = dict(source_cfg or {})
        for attr in ("verify", "key"):
            if not callable(getattr(module, attr, None)):
                raise PluginError(f"source {name}: plugin defines no {attr}()")
        if not hasattr(module, "auth_headers"):
            raise PluginError(f"source {name}: plugin declares no auth_headers")
        self.auth_headers = _names(module.auth_headers, "auth_headers")
        keep = self.cfg.get("keep_headers")
        self.keep_headers = _names(keep if keep is not None else getattr(module, "keep_headers", ()), "keep_headers")
        self.secret_names = _names(getattr(module, "secrets", ()), "secrets")
        self.challenge_unauthenticated = bool(getattr(module, "challenge_unauthenticated", False))
        self.span = getattr(module, "span", None)
        self.expect_every_s = getattr(module, "expect_every_s", None)
        self.sender_retries = getattr(module, "sender_retries", None)
        self._challenge = getattr(module, "challenge", None)
        self._action = self.cfg.get("action") or getattr(module, "action", None)
        stripped = {h.lower() for h in self.auth_headers} | {h.lower() for h in ALWAYS_STRIPPED}
        self._stripped = stripped
        # keep_headers can never carry an auth header (`check` refuses it; this enforces it)
        self.keep_headers = tuple(h for h in self.keep_headers if h.lower() not in stripped)

    # -- secrets and config
    def secrets(self) -> Secrets:
        return Secrets(self.cfg.get("secrets") or {}, self.name)

    def public_config(self) -> dict:
        """The source's config table without secret references (``req.config``)."""
        return {k: v for k, v in self.cfg.items() if k != "secrets"}

    # -- the stages, in intake order
    @property
    def has_challenge(self) -> bool:
        return callable(self._challenge)

    def verify(self, req: Request, secrets: Mapping | None = None) -> bool:
        return self.module.verify(req, self.secrets() if secrets is None else secrets) is True

    def challenge(self, req: Request, secrets: Mapping | None = None) -> Response | None:
        if not self.has_challenge:
            return None
        if self.challenge_unauthenticated:
            secrets = NoSecrets()
        elif secrets is None:
            secrets = self.secrets()
        out = self._challenge(req, secrets)
        if out is not None and not isinstance(out, Response):
            raise PluginError(f"source {self.name}: challenge() must return a Response or None")
        return out

    def ignore(self, req: Request) -> str | None:
        f = getattr(self.module, "ignore", None)
        if f is None:
            return None
        r = f(req)
        return None if r is None or r is False or r == "" else str(r)

    def payload(self, req: Request) -> Any:
        f = getattr(self.module, "payload", None)
        if f is not None:
            return f(req)
        p = req.json()
        return {} if p is None else p

    def key(self, payload: Any, req: Request) -> str:
        return self.module.key(payload, req)

    def dedup_id(self, payload: Any, req: Request) -> str | None:
        f = getattr(self.module, "dedup_id", None)
        if f is None:
            return None
        v = f(payload, req)
        return None if v is None or v == "" else str(v)

    def event_type(self, payload: Any, req: Request) -> str:
        f = getattr(self.module, "event_type", None)
        if f is None:
            return ""
        v = f(payload, req)
        return "" if v is None else str(v)

    def attrs(self, payload: Any, req: Request) -> dict:
        """The plugin's ``attrs`` plus ``route_hint`` (the ``X-Hookspool-Route`` header the
        reverse proxy sets per provider-side URL) unless the plugin set one itself, so a
        config route can say ``match = {route_hint = "ci"}`` without plugin code."""
        f = getattr(self.module, "attrs", None)
        out = dict(f(payload, req) or {}) if f is not None else {}
        if req.route_hint and "route_hint" not in out:
            out["route_hint"] = req.route_hint
        return out

    def action(self, payload: Any, req: Request) -> str | None:
        a = self._action
        if callable(a):
            a = a(payload, req)
        return a if isinstance(a, str) and a else None

    # -- headers
    def kept_headers(self, req: Request) -> dict[str, str] | None:
        if not self.keep_headers:
            return None
        out = {}
        for h in self.keep_headers:
            v = req.header(h)
            if v:
                out[h] = v
        return out or None

    def quarantine_headers(self, req: Request) -> dict[str, str]:
        """Every request header except auth headers, plus ``:method`` and ``:path`` (no
        query string: tokens live there)."""
        out: dict[str, str] = {":method": req.method, ":path": req.path}
        for k, v in req.headers:
            if k.lower() in self._stripped or k.startswith(":"):
                continue
            out.setdefault(k, v)
        return out

    def classify(self, req: Request, default_route_cfg: Mapping | None = None) -> "Classified | str":
        """ignore → payload → key/dedup_id/event_type/attrs → route resolution. Returns the
        ignore reason (str) or a Classified. Raises whatever the plugin raises."""
        reason = self.ignore(req)
        if reason is not None:
            return reason
        payload = self.payload(req)
        key = self.key(payload, req)
        dd = self.dedup_id(payload, req)
        et = self.event_type(payload, req)
        attrs = self.attrs(payload, req)
        return Classified(payload=payload, key=key, dedup_id=dd, event_type=et, attrs=attrs,
                          default_action=self.action(payload, req))


@dataclass
class Classified:
    payload: Any
    key: Any
    dedup_id: str | None
    event_type: str
    attrs: dict
    default_action: str | None


def load_source(name: str, source_cfg: Mapping[str, Any] | None,
                loader: Callable[[str, str | None], Any] = _config.default_plugin_loader) -> Source:
    cfg = dict(source_cfg or {})
    return Source(name, _config.bind_plugin(loader(name, cfg.get("module")), name, cfg), cfg)


# ---- .http captures ------------------------------------------------------------------------------------
@dataclass
class RawRequest:
    method: str
    target: str
    headers: list[tuple[str, str]]
    body: bytes = b""
    meta: dict[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str:
        for k, v in self.headers:
            if k.lower() == name.lower():
                return v
        return ""

    def set(self, name: str, value: str | None) -> "RawRequest":
        hs = [(a, b) for a, b in self.headers if a.lower() != name.lower()]
        if value is not None:
            hs.append((name, value))
        return RawRequest(self.method, self.target, hs, self.body, dict(self.meta))

    def with_body(self, body: bytes) -> "RawRequest":
        return RawRequest(self.method, self.target, list(self.headers), bytes(body), dict(self.meta))


def parse_capture(data: bytes) -> RawRequest:
    """Parse a ``.http`` capture (see the module docstring)."""
    meta: dict[str, str] = {}
    rest = data
    while True:
        line, sep, after = rest.partition(b"\n")
        s = line.rstrip(b"\r").decode("utf-8")
        if s.startswith("#"):
            k, colon, v = s[1:].partition(":")
            if colon and k.strip():
                meta[k.strip().lower()] = v.strip()
            rest = after
            continue
        if not s.strip() and sep:
            rest = after
            continue
        break
    head, sep, body = rest.partition(b"\r\n\r\n")
    if not sep:
        head, sep, body = rest.partition(b"\n\n")
    lines = head.decode("utf-8").replace("\r\n", "\n").split("\n")
    start = lines[0].split()
    if len(start) < 2:
        raise ValueError("capture has no request line")
    method, target = start[0].upper(), start[1]
    headers: list[tuple[str, str]] = []
    for ln in lines[1:]:
        if not ln.strip():
            continue
        k, colon, v = ln.partition(":")
        if not colon:
            raise ValueError(f"bad header line in capture: {k[:40]!r}")
        headers.append((k.strip(), v.strip()))
    cl = next((v for k, v in headers if k.lower() == "content-length"), None)
    if cl is not None and cl.isdigit():
        body = body[:int(cl)]
    elif body.endswith(b"\r\n"):
        body = body[:-2]
    elif body.endswith(b"\n"):
        body = body[:-1]
    return RawRequest(method, target, headers, body, meta)


def format_capture(raw: RawRequest) -> bytes:
    """The inverse of ``parse_capture`` (sets Content-Length when there is a body)."""
    out = [f"# {k}: {v}" for k, v in raw.meta.items()]
    out.append(f"{raw.method} {raw.target} HTTP/1.1")
    hs = [(k, v) for k, v in raw.headers if k.lower() != "content-length"]
    if raw.body or raw.method in ("POST", "PUT", "PATCH"):
        hs.append(("Content-Length", str(len(raw.body))))
    out += [f"{k}: {v}" for k, v in hs]
    return ("\n".join(out) + "\n\n").encode("utf-8") + raw.body + b"\n"
