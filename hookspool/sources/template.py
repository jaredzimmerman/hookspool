"""The ``template`` source: a webhook source defined entirely in TOML, with no Python.

Most senders are a signed JSON POST. This plugin covers them with config::

    [sources.mysender]
    module = "hookspool.sources.template"
    scheme = "hmac-sha256-hex"          # how the sender proves it is the sender
    header = "X-Hub-Signature-256"
    prefix = "sha256="
    key = "repository.full_name"        # what events coalesce on
    dedup = "header:X-GitHub-Delivery"  # optional
    ignore = "header:X-GitHub-Event=ping"
    action = "hermes:mysender"
    secrets.secret = { file = "/etc/hookspool/secrets/mysender" }

Verification schemes (all constant-time, through ``hookspool.verify``):

``hmac-sha256-hex``        ``header`` holds the hex HMAC-SHA256 of the body, optionally behind ``prefix``
``hmac-sha256-base64``     the same, base64 encoded
``hmac-sha1-base64-url``   ``header`` holds base64 HMAC-SHA1 of the body followed by ``callback_url``
``shared-secret-header``   ``header`` holds the secret itself, optionally behind ``prefix``
``url-token``              the query parameter ``param`` (default ``token``) holds the secret

Selectors (``key``, ``dedup``, ``event_type``, ``attrs`` values, and the left side of ``ignore``):

``a.b.0.c``       a dotted path into the JSON body (object keys and list indexes)
``header:Name``   a request header
``literal:text``  a constant (``key`` and ``event_type`` only)
``body_sha256``   the SHA-256 of the raw body (``dedup`` only)

There is no expression language: a selector reads one value, ``key`` may list several (joined
with ``:``), and ``ignore`` entries are ``<selector>=<value>`` equality tests. A ``key`` that
cannot be read raises ``BadPayload``, so the intake quarantines the request instead of losing it.
Anything this cannot say is a job for a code plugin (ARCHITECTURE section 14.1).

The plugin is bound once per source (``bind``): the module-level names below are shared, the
bound module that ``Source`` wraps carries ``auth_headers``, ``secrets`` and the compiled stages.
"""
from __future__ import annotations

import re
import types
from collections.abc import Mapping
from typing import Any

from hookspool.plugin import BadPayload, PluginError, Request, Response, Secrets, dedup as _dedup, get_path, \
    verify as v

SCHEMES = ("hmac-sha256-hex", "hmac-sha256-base64", "hmac-sha1-base64-url", "shared-secret-header", "url-token")
config_keys = ("scheme", "header", "prefix", "param", "callback_url", "head_handshake", "key", "dedup",
               "ignore", "event_type", "attrs", "sender_retries")

_SCHEME_KEYS = {
    "hmac-sha256-hex": {"header", "prefix"},
    "hmac-sha256-base64": {"header", "prefix"},
    "hmac-sha1-base64-url": {"header", "callback_url"},
    "shared-secret-header": {"header", "prefix"},
    "url-token": {"param"},
}
_REQUIRED = {"hmac-sha256-hex": ("header",), "hmac-sha256-base64": ("header",),
             "hmac-sha1-base64-url": ("header", "callback_url"), "shared-secret-header": ("header",),
             "url-token": ()}
_TOKEN = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_PATH = re.compile(r"^[^\s.=:]+(\.[^\s.=:]+)*$")
_ATTR = re.compile(r"^[A-Za-z0-9_-]+$")
_PARAM = re.compile(r"^[A-Za-z0-9_.~-]+$")


class TemplateConfigError(PluginError):
    """The source table is not a valid template; ``errors`` lists every problem (no secrets)."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = list(errors)


# ---- selectors -------------------------------------------------------------------------------------------
def _scalar(val: Any) -> str | None:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (str, int, float)):
        s = str(val)
        return s or None
    return None


class Selector:
    """One value read from the request: a header, a payload path, a constant or the body hash."""

    __slots__ = ("kind", "arg", "text")

    def __init__(self, kind: str, arg: str, text: str):
        self.kind, self.arg, self.text = kind, arg, text

    def get(self, payload: Any, req: Request) -> str | None:
        if self.kind == "header":
            return req.header(self.arg) or None
        if self.kind == "literal":
            return self.arg
        if self.kind == "body_sha256":
            return _dedup.body_sha256(payload, req)
        return _scalar(get_path(payload, self.arg))


def _selector(text: Any, allowed: tuple[str, ...]) -> Selector:
    """Parse a selector or raise ValueError with a short reason."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("must be a non-empty string")
    if text == "body_sha256":
        kind, arg = "body_sha256", ""
    elif text.startswith("header:"):
        kind, arg = "header", text[7:]
        if not _TOKEN.match(arg):
            raise ValueError(f"{text!r}: not a header name")
    elif text.startswith("literal:"):
        kind, arg = "literal", text[8:]
        if not arg:
            raise ValueError(f"{text!r}: empty constant")
    else:
        kind, arg = "path", text
        if not _PATH.match(arg):
            raise ValueError(f"{text!r}: not a dotted path, header:Name, literal:text or body_sha256")
    if kind not in allowed:
        raise ValueError(f"{text!r}: a {kind.replace('_', ' ')} selector is not allowed here")
    return Selector(kind, arg, text)


def _as_list(val: Any) -> list | None:
    if isinstance(val, str):
        return [val]
    if isinstance(val, list) and val:
        return val
    return None


# ---- the compiled source ----------------------------------------------------------------------------------
class Spec:
    """A validated template config. ``errors`` is empty for a usable one."""

    def __init__(self, name: str, cfg: Mapping[str, Any]):
        self.name = name
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.scheme = cfg.get("scheme")
        self.header = ""
        self.prefix = ""
        self.param = "token"
        self.callback_url = ""
        self.head_handshake = False
        self.key_sel: list[Selector] = []
        self.dedup_sel: Selector | None = None
        self.event_sel: Selector | None = None
        self.attr_sels: dict[str, Selector] = {}
        self.ignore_rules: list[tuple[Selector, str, str]] = []
        self.sender_retries: bool | None = None
        self._compile(cfg)

    def _err(self, field: str, msg: str) -> None:
        self.errors.append(f"{field}: {msg}")

    def _compile(self, cfg: Mapping[str, Any]) -> None:
        scheme = self.scheme
        if scheme not in SCHEMES:
            self._err("scheme", f"required, one of {', '.join(SCHEMES)}")
            scheme = None
        if scheme:
            for k in ("header", "prefix", "param", "callback_url"):
                if k in cfg and k not in _SCHEME_KEYS[scheme]:
                    self._err(k, f"not used by scheme {scheme}")
            for k in _REQUIRED[scheme]:
                if k not in cfg:
                    self._err(k, f"required by scheme {scheme}")
        h = cfg.get("header")
        if h is not None:
            if isinstance(h, str) and _TOKEN.match(h):
                self.header = h
            else:
                self._err("header", "not a valid header name")
        p = cfg.get("prefix")
        if p is not None:
            if isinstance(p, str) and p and p.isprintable() and p.isascii():
                self.prefix = p
            else:
                self._err("prefix", "must be a non-empty printable ASCII string")
        q = cfg.get("param")
        if q is not None:
            if isinstance(q, str) and _PARAM.match(q):
                self.param = q
            else:
                self._err("param", "must be a query parameter name")
        cb = cfg.get("callback_url")
        if cb is not None:
            if isinstance(cb, str) and re.match(r"^https?://[^\s]+$", cb):
                self.callback_url = cb
            else:
                self._err("callback_url", "must be the http(s) URL the sender was given")
        for fld in ("head_handshake", "sender_retries"):
            if fld in cfg and not isinstance(cfg[fld], bool):
                self._err(fld, "must be true or false")
        self.head_handshake = cfg.get("head_handshake") is True
        self.sender_retries = cfg["sender_retries"] if isinstance(cfg.get("sender_retries"), bool) else None
        # key
        keys = _as_list(cfg.get("key"))
        if keys is None:
            self._err("key", "required: a selector or a list of selectors")
        else:
            for item in keys:
                try:
                    self.key_sel.append(_selector(item, ("path", "header", "literal")))
                except ValueError as e:
                    self._err("key", str(e))
        for fld, attr, allowed in (("dedup", "dedup_sel", ("path", "header", "body_sha256")),
                                   ("event_type", "event_sel", ("path", "header", "literal"))):
            if cfg.get(fld) is not None:
                try:
                    setattr(self, attr, _selector(cfg[fld], allowed))
                except ValueError as e:
                    self._err(fld, str(e))
        attrs = cfg.get("attrs")
        if attrs is not None:
            if not isinstance(attrs, Mapping):
                self._err("attrs", "must be a table of name = selector")
            else:
                for n, text in attrs.items():
                    if not isinstance(n, str) or not _ATTR.match(n):
                        self._err("attrs", f"{n!r}: not a valid attribute name")
                        continue
                    try:
                        self.attr_sels[n] = _selector(text, ("path", "header", "literal"))
                    except ValueError as e:
                        self._err(f"attrs.{n}", str(e))
        if cfg.get("ignore") is not None:
            rules = _as_list(cfg["ignore"])
            if rules is None:
                self._err("ignore", "must be \"<selector>=<value>\" or a list of them")
            else:
                for item in rules:
                    sel_text, eq, val = item.partition("=") if isinstance(item, str) else ("", "", "")
                    if not eq or not val:
                        self._err("ignore", f"{item!r}: expected \"<selector>=<value>\"")
                        continue
                    try:
                        self.ignore_rules.append((_selector(sel_text, ("path", "header")), val, item))
                    except ValueError as e:
                        self._err("ignore", str(e))
        if scheme == "url-token":
            self.warnings.append("scheme url-token puts the secret in the URL; prefer a signature scheme when"
                                 " the sender offers one")
        if scheme == "hmac-sha1-base64-url" and self.callback_url.startswith("http://"):
            self.warnings.append("callback_url is plain http; senders that sign the URL usually require https")

    # -- stages
    @property
    def auth_headers(self) -> tuple[str, ...]:
        return (self.header,) if self.header else ()

    def verify(self, req: Request, secrets: Mapping[str, str]) -> bool:
        scheme = self.scheme
        if scheme == "hmac-sha256-hex":
            if self.prefix:
                return v.hmac_prefixed(req.header(self.header), self.prefix, secrets["secret"], req.body)
            return v.hmac_hex(req.header(self.header), secrets["secret"], req.body)
        if scheme == "hmac-sha256-base64":
            if self.prefix:
                return v.hmac_prefixed(req.header(self.header), self.prefix, secrets["secret"], req.body,
                                       encoding="base64")
            return v.hmac_b64(req.header(self.header), secrets["secret"], req.body)
        if scheme == "hmac-sha1-base64-url":
            return v.hmac_b64(req.header(self.header), secrets["secret"],
                              req.body + self.callback_url.encode("utf-8"), algo="sha1")
        if scheme == "shared-secret-header":
            got = req.header(self.header)
            if self.prefix:
                if not got.startswith(self.prefix):
                    return False
                got = got[len(self.prefix):]
            return v.static_token(got, secrets["secret"])
        if scheme == "url-token":
            return v.static_token(req.query.get(self.param, ""), secrets["secret"])
        return False

    def challenge(self, req: Request, secrets: Mapping[str, str]) -> Response | None:
        return Response(200) if req.method == "HEAD" else None

    def ignore(self, req: Request) -> str | None:
        for sel, want, text in self.ignore_rules:
            payload = req.json() if sel.kind == "path" else None
            if sel.get(payload, req) == want:
                return text
        return None

    def key(self, payload: Any, req: Request) -> str:
        parts = []
        for sel in self.key_sel:
            val = sel.get(payload, req)
            if val is None:
                raise BadPayload(f"key {sel.text} not found in the request")
            parts.append(val)
        return ":".join(parts)

    def dedup_id(self, payload: Any, req: Request) -> str | None:
        return self.dedup_sel.get(payload, req) if self.dedup_sel else None

    def event_type(self, payload: Any, req: Request) -> str:
        return (self.event_sel.get(payload, req) or "") if self.event_sel else ""

    def attrs(self, payload: Any, req: Request) -> dict:
        out = {}
        for n, sel in self.attr_sels.items():
            val = sel.get(payload, req)
            if val is not None:
                out[n] = val
        return out


# ---- binding -----------------------------------------------------------------------------------------------
def bind(name: str, cfg: Mapping[str, Any]) -> types.ModuleType:
    """The plugin module for one configured source. Raises ``TemplateConfigError`` listing
    every problem in the table."""
    spec = Spec(name, cfg)
    if spec.errors:
        raise TemplateConfigError(spec.errors)
    m = types.ModuleType(f"hookspool.sources.template[{name}]")
    m.name = name
    m.secrets = ("secret",)
    m.auth_headers = spec.auth_headers
    m.span = None
    m.sender_retries = spec.sender_retries
    m.config_keys = config_keys
    m.config_warnings = tuple(spec.warnings)
    m.spec = spec
    m.verify = spec.verify
    m.key = spec.key
    if spec.head_handshake:
        m.challenge_unauthenticated = True
        m.challenge = spec.challenge
    if spec.ignore_rules:
        m.ignore = spec.ignore
    if spec.dedup_sel:
        m.dedup_id = spec.dedup_id
    if spec.event_sel:
        m.event_type = spec.event_type
    if spec.attr_sels:
        m.attrs = spec.attrs
    if isinstance(cfg.get("expect_every_s"), (int, float)) and not isinstance(cfg.get("expect_every_s"), bool):
        m.expect_every_s = cfg["expect_every_s"]
    return m


def verify(req: Request, secrets: Secrets) -> bool:
    """Verify with the config the request carries (``req.config``). The intake calls the
    bound module's ``verify`` instead; this form exists for callers that hold only a request."""
    spec = Spec("template", req.config)
    return False if spec.errors else spec.verify(req, secrets)
