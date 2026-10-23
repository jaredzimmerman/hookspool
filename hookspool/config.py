"""hookspool configuration (contract C5; ARCHITECTURE §15).

One TOML file (``config.toml`` in the state directory). ``load_config`` merges the
file over ``DEFAULTS``; ``check`` validates it and returns every error and warning
(``hookspool check`` exits 2 on errors). Unknown keys are errors in ``check`` and
are otherwise ignored at runtime.

Window fields (``quiet_s``, ``max_s``, ``max_events``, ``max_bytes``) are inherited
per field: route -> source -> the source's ``span`` preset -> ``[coalesce]``.

Path precedence: CLI flag -> environment (``HOOKSPOOL_HOME``, ``HOOKSPOOL_CONFIG``,
``HOOKSPOOL_DB``) -> config -> default.

Secrets are references (``{file = ...}``, ``{env = ...}``, ``{env_file = ..., key = ...}``)
resolved at use and never cached, logged or stored.
"""
from __future__ import annotations

import copy
import importlib
import json
import os
import stat
import tomllib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

DEFAULT_HOME = "/var/lib/hookspool"
WINDOW_FIELDS = ("quiet_s", "max_s", "max_events", "max_bytes")
DAY = 86400.0
LEASE_MARGIN_S = 5.0                 # the worker deadline is lease_until - this (dispatch.DEADLINE_MARGIN_S)

DEFAULTS: dict = {
    "spool": {"home": None, "db": "spool.db", "store_raw": True},
    "intake": {"listen": "127.0.0.1:8650", "max_body_bytes": 1048576, "busy_timeout_s": 10, "threads": 16},
    "dispatch": {
        "parallel": 4, "poll_s": 1.0, "heartbeat_s": 10, "lease_grace_s": 60, "max_attempts": 64,
        "backoff_base_s": 15, "backoff_cap_s": 300,
        "breaker": {"trip_after": 3, "cooldown_initial_s": 30, "cooldown_max_s": 600},
    },
    "coalesce": {"quiet_s": 0, "max_s": 0, "max_events": 500, "max_bytes": 786432},
    "limits": {"min_free_bytes": 536870912},
    "retention": {"delivered_days": 7, "attempts_days": 30, "dropped_days": 7, "issues_days": 90},
    "alerts": {"argv": ["logger", "-t", "hookspool"], "dry": False, "rate_limit_s": 1800, "timeout_s": 60},
    "hermes": {
        "base_url": "http://127.0.0.1:8644", "subscriptions_path": "~/.hermes/webhook_subscriptions.json",
        "timeout_s": 30, "max_body_bytes": 1048576, "legacy_signature": True,
    },
    "spans": {
        "burst": {"quiet_s": 120, "max_s": 600},       # code and CI
        "mail": {"quiet_s": 30, "max_s": 120},         # mail, boards, tickets
        "calendar": {"quiet_s": 45, "max_s": 180},     # calendar edits
        "sync": {"quiet_s": 60, "max_s": 300},         # bank and data syncs
        "immediate": {"quiet_s": 0, "max_s": 0},       # meetings, security
    },
    "sources": {},
    "routes": [],
    "destinations": {},
}

DEFAULT_DEDUP_WINDOW_S = 86400.0
DEFAULT_MAX_INFLIGHT = {"hermes": 2, "script": 1, "http": 4}
HTTP_DEFAULT_MAX_BODY = 1048576
RESERVED_DESTINATIONS = ("quarantine",)

# Known keys per section. A value of None means "free-form table" (spans, match, headers).
_SECTION_KEYS: dict[str, set[str]] = {
    "spool": {"home", "db", "store_raw"},
    "intake": {"listen", "max_body_bytes", "busy_timeout_s", "threads"},
    "dispatch": {"parallel", "poll_s", "heartbeat_s", "lease_grace_s", "max_attempts", "backoff_base_s",
                 "backoff_cap_s", "breaker"},
    "coalesce": set(WINDOW_FIELDS),
    "limits": {"min_free_bytes"},
    "retention": {"delivered_days", "attempts_days", "dropped_days", "issues_days"},
    "alerts": {"argv", "dry", "rate_limit_s", "timeout_s"},
    "hermes": {"base_url", "subscriptions_path", "timeout_s", "max_body_bytes", "legacy_signature"},
}
_BREAKER_KEYS = {"trip_after", "cooldown_initial_s", "cooldown_max_s"}
_TOP_KEYS = set(_SECTION_KEYS) | {"spans", "sources", "routes", "destinations"}
SOURCE_KEYS = {"module", "span", "secrets", "dedup_window_s", "expect_every_s", "max_body_bytes", "action",
               "keep_headers", "store_raw", "event_header", *WINDOW_FIELDS}
ROUTE_KEYS = {"name", "source", "match", "destination", *WINDOW_FIELDS}
_DEST_COMMON = {"type", "module", "timeout_s", "max_body_bytes", "max_inflight"}
DEST_KEYS = {
    "hermes": _DEST_COMMON,
    "script": _DEST_COMMON | {"argv", "cwd", "env_file", "state_dir"},
    "http": _DEST_COMMON | {"url", "secret", "secret_previous", "headers", "allow_insecure"},
}
# Keys that existed in the v1 draft or the first v2 draft and were removed on purpose.
REMOVED_KEYS = {
    "limits.max_pending_batches", "limits.max_db_bytes", "limits.plugin_error_streak",
    "retention.archive_dir", "dispatch.poison_after", "dispatch.dead_after_s", "delay_s", "rate_per_min",
    "idempotency", "paused",
}


class ConfigError(ValueError):
    """The config file cannot be read or parsed (exit 2)."""


# ---- paths ---------------------------------------------------------------------------------
def _expand(p: str) -> str:
    return os.path.abspath(os.path.expanduser(p))


def home_dir(cfg: dict | None = None, cli_home: str | None = None) -> str:
    """State directory: --home, then HOOKSPOOL_HOME, then [spool].home, then the default."""
    h = cli_home or os.environ.get("HOOKSPOOL_HOME") or ((cfg or {}).get("spool") or {}).get("home")
    return _expand(h or DEFAULT_HOME)


def config_path(cli_config: str | None = None, cli_home: str | None = None) -> str:
    """Config file: --config, then HOOKSPOOL_CONFIG, then <home>/config.toml."""
    p = cli_config or os.environ.get("HOOKSPOOL_CONFIG")
    if p:
        return _expand(p)
    return os.path.join(home_dir(None, cli_home), "config.toml")


def db_path(cfg: dict | None = None, cli_db: str | None = None, cli_home: str | None = None) -> str:
    """Spool file: --db, then HOOKSPOOL_DB, then [spool].db relative to the home directory."""
    p = cli_db or os.environ.get("HOOKSPOOL_DB")
    if p:
        return _expand(p)
    db = ((cfg or {}).get("spool") or {}).get("db") or "spool.db"
    return os.path.join(home_dir(cfg, cli_home), os.path.expanduser(db))


def alerts_dry(cfg: dict) -> bool:
    if os.environ.get("HOOKSPOOL_ALERT_DRY", "") not in ("", "0"):
        return True
    return bool((cfg.get("alerts") or {}).get("dry"))


def log_format() -> str:
    """``HOOKSPOOL_LOG``: ``json`` or ``text`` (default)."""
    return "json" if os.environ.get("HOOKSPOOL_LOG", "").strip().lower() == "json" else "text"


# ---- loading -------------------------------------------------------------------------------
def merge(base: dict, over: dict) -> dict:
    """Deep merge of tables; lists and scalars in ``over`` replace those in ``base``."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def parse_toml(text: str, origin: str = "<config>") -> dict:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{origin} is not valid TOML: {e}") from None
    return merge(DEFAULTS, data)


def load_config(path: str | None = None, *, cli_home: str | None = None) -> dict:
    """DEFAULTS merged with the TOML file. A missing file means defaults (no sources).

    A file that exists but does not parse raises ConfigError: silently running with
    defaults would hide a broken deploy. The returned dict carries ``_path`` (the file
    that was read, or None).
    """
    p = path or config_path(None, cli_home)
    try:
        with open(p, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        cfg = copy.deepcopy(DEFAULTS)
        cfg["_path"] = None
        return cfg
    except OSError as e:
        raise ConfigError(f"cannot read config {p}: {e.__class__.__name__}") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"{p} is not UTF-8") from None
    cfg = parse_toml(text, p)
    cfg["_path"] = p
    return cfg


# ---- windows and routes ------------------------------------------------------------------------
def source_cfg(cfg: dict, source: str) -> dict:
    return (cfg.get("sources") or {}).get(source) or {}


def route_cfg(cfg: dict, route: str | None) -> dict | None:
    if not route:
        return None
    for r in cfg.get("routes") or []:
        if isinstance(r, dict) and r.get("name") == route:
            return r
    return None


def window_for(cfg: dict, source: str, route: str | None = None, *,
               plugin_span: str | None = None) -> dict[str, float | int]:
    """Coalesce window for an event, inherited per field (ARCHITECTURE §5).

    route -> source -> span preset (the source's ``span``, else the plugin's default
    ``plugin_span``) -> ``[coalesce]``. Presets set ``quiet_s`` and ``max_s`` only.
    """
    r = route_cfg(cfg, route) or {}
    s = source_cfg(cfg, source)
    span_name = s.get("span") or plugin_span
    preset = ((cfg.get("spans") or {}).get(span_name) or {}) if span_name else {}
    base = merge(DEFAULTS["coalesce"], cfg.get("coalesce") or {})
    out: dict[str, float | int] = {}
    for f in WINDOW_FIELDS:
        for layer in (r, s, preset if f in ("quiet_s", "max_s") else {}, base):
            if layer.get(f) is not None:
                out[f] = layer[f]
                break
    out["quiet_s"] = float(out["quiet_s"])
    out["max_s"] = float(out["max_s"])
    out["max_events"] = int(out["max_events"])
    out["max_bytes"] = int(out["max_bytes"])
    return out


def dedup_window_s(cfg: dict, source: str) -> float:
    v = source_cfg(cfg, source).get("dedup_window_s")
    return DEFAULT_DEDUP_WINDOW_S if v is None else float(v)


def _match(match: dict | None, attrs: dict) -> bool:
    for k, v in (match or {}).items():
        if k not in attrs or str(attrs[k]) != str(v):
            return False
    return True


def resolve_route(cfg: dict, source: str, attrs: dict | None, default_action: str | None) -> tuple[str, str | None]:
    """(route name, destination). First ``[[routes]]`` entry whose source matches and whose
    ``match`` is a subset of ``attrs`` wins; otherwise the implicit route ``<source>`` with the
    source's ``action`` (config) or the plugin's ``default_action``."""
    attrs = attrs or {}
    for r in cfg.get("routes") or []:
        if isinstance(r, dict) and r.get("source") == source and _match(r.get("match"), attrs):
            return str(r["name"]), r.get("destination") or source_cfg(cfg, source).get("action") or default_action
    return source, source_cfg(cfg, source).get("action") or default_action


def route_destination(cfg: dict, route: str) -> str | None:
    """Destination of a configured route, or the action of a source's implicit route."""
    r = route_cfg(cfg, route)
    if r is not None:
        return r.get("destination") or source_cfg(cfg, r.get("source", "")).get("action")
    return source_cfg(cfg, route).get("action")


def destination_type(cfg: dict, name: str) -> str | None:
    d = (cfg.get("destinations") or {}).get(name) or {}
    if d.get("type"):
        return str(d["type"])
    if d.get("module"):
        return "module"
    if name.startswith("hermes:"):
        return "hermes"
    if name.startswith("script:"):
        return "script"
    return None


def destination_max_body_bytes(cfg: dict, name: str) -> int | None:
    """What the receiver accepts (None = unbounded, the script default)."""
    d = (cfg.get("destinations") or {}).get(name) or {}
    if d.get("max_body_bytes") is not None:
        return int(d["max_body_bytes"])
    t = destination_type(cfg, name)
    if t == "hermes":
        return int((cfg.get("hermes") or {}).get("max_body_bytes") or HTTP_DEFAULT_MAX_BODY)
    if t == "http":
        return HTTP_DEFAULT_MAX_BODY
    return None


def source_max_body_bytes(cfg: dict, source: str, *, plugin_span: str | None = None) -> int:
    """Effective request-body cap for a source (the intake answers 413 above it).

    An explicit ``[sources.<name>].max_body_bytes`` is used as is (``check`` requires it to
    fit every route the source feeds). Without one, ``[intake].max_body_bytes`` is clamped
    to the smallest ``max_bytes`` of those routes, so one event always fits its document.
    """
    s = source_cfg(cfg, source)
    if s.get("max_body_bytes") is not None:
        return int(s["max_body_bytes"])
    cap = int((cfg.get("intake") or {}).get("max_body_bytes") or DEFAULTS["intake"]["max_body_bytes"])
    routes = [r.get("name") for r in cfg.get("routes") or [] if isinstance(r, dict) and r.get("source") == source]
    for route in [None, *routes]:
        cap = min(cap, int(window_for(cfg, source, route, plugin_span=plugin_span)["max_bytes"]))
    return cap


# ---- secrets -------------------------------------------------------------------------------------
class SecretError(ValueError):
    """A secret reference cannot be resolved. The message never contains the value."""


READ_FILE_MAX = 16 * 1024 * 1024


class NotRegularFile(OSError):
    """A secret, env or subscriptions path that is not a regular file (a FIFO, a device)."""


def read_regular_text(path: str, encoding: str = "utf-8") -> str:
    """The whole text of a regular file. Opened non-blocking and checked before any read, so a
    FIFO or a device in a secret path is an error at once instead of a reader that never
    returns (an intake thread waiting on verify, or a delivery outliving its lease)."""
    fd = os.open(os.path.expanduser(str(path)), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOCTTY", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise NotRegularFile(f"{path} is not a regular file")
        if st.st_size > READ_FILE_MAX:
            raise NotRegularFile(f"{path} is larger than {READ_FILE_MAX} bytes")
        chunks, total = [], 0
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            total += len(b)
            if total > READ_FILE_MAX:
                raise NotRegularFile(f"{path} is larger than {READ_FILE_MAX} bytes")
            chunks.append(b)
    finally:
        os.close(fd)
    return b"".join(chunks).decode(encoding)


def read_env_file(path: str) -> dict:
    """KEY=VALUE lines (optional ``export``, optional quotes, ``#`` comments)."""
    out = {}
    for line in read_regular_text(path).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        k, sep, v = line.partition("=")
        if not sep or not k.strip():
            continue
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def secret_ref_kind(ref: Any) -> str:
    """'file', 'env' or 'env_file'; raises SecretError for anything else (including a literal)."""
    if not isinstance(ref, dict):
        raise SecretError("a secret must be a reference table ({file=...}, {env=...} or {env_file=..., key=...}),"
                          " never a literal value")
    keys = set(ref)
    if keys == {"file"}:
        return "file"
    if keys == {"env"}:
        return "env"
    if keys == {"env_file", "key"}:
        return "env_file"
    raise SecretError(f"secret reference has keys {sorted(keys)}; expected file, env, or env_file + key")


def resolve_secret(ref: Any) -> str:
    """Read the secret now (no caching, so file and env_file refs rotate without a restart)."""
    kind = secret_ref_kind(ref)
    try:
        if kind == "file":
            v = read_regular_text(ref["file"]).strip("\r\n")
        elif kind == "env":
            v = os.environ.get(str(ref["env"]))
            if v is None:
                raise SecretError(f"environment variable {ref['env']} is not set")
        else:
            env = read_env_file(ref["env_file"])
            if ref["key"] not in env:
                raise SecretError(f"{ref['env_file']} has no key {ref['key']}")
            v = env[ref["key"]]
    except (OSError, UnicodeDecodeError) as e:
        raise SecretError(f"cannot read secret ({kind}): {e.__class__.__name__}") from None
    if not v:
        raise SecretError(f"secret ({kind}) is empty")
    return v


# ---- check ---------------------------------------------------------------------------------------
@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def _unknown(report: Report, where: str, table: Any, allowed: Iterable[str]) -> None:
    if not isinstance(table, dict):
        report.err(f"{where} must be a table")
        return
    allowed = set(allowed)
    for k in table:
        if k in allowed or (where == "" and k == "_path"):
            continue
        dotted = f"{where}.{k}" if where else k
        tail = dotted.split(".", 1)[-1] if "." in dotted else dotted
        if dotted in REMOVED_KEYS or k in REMOVED_KEYS or tail in REMOVED_KEYS:
            report.err(f"{dotted}: removed in v2 (see ARCHITECTURE §15)")
        else:
            report.err(f"{dotted}: unknown key")


def _num(report: Report, where: str, v: Any, *, minimum: float = 0, integer: bool = False,
         allow_none: bool = False) -> bool:
    if v is None and allow_none:
        return True
    ok = not isinstance(v, bool) and isinstance(v, (int, float)) and (not integer or isinstance(v, int))
    if not ok or v < minimum:
        kind = "an integer" if integer else "a number"
        report.err(f"{where}: must be {kind} >= {minimum}")
        return False
    return True


def default_plugin_loader(name: str, module: str | None):
    return importlib.import_module(module or f"hookspool.sources.{name}")


def bind_plugin(module: Any, name: str, source_cfg: dict | None):
    """A plugin module may define ``bind(name, source_cfg)`` and return the module-like object
    to use for that one source. This is how a configurable plugin (the ``template`` source)
    gets per-source attributes such as ``auth_headers``. Plain plugins have no ``bind``."""
    b = getattr(module, "bind", None)
    return b(name, dict(source_cfg or {})) if callable(b) else module


def _check_secret(report: Report, where: str, ref: Any) -> None:
    try:
        kind = secret_ref_kind(ref)
    except SecretError as e:
        report.err(f"{where}: {e}")
        return
    if kind == "env":
        report.warn(f"{where}: env refs do not rotate without a restart")
    path = ref.get("file") or ref.get("env_file")
    if path:
        p = os.path.expanduser(str(path))
        try:
            st = os.stat(p)
        except OSError:
            report.err(f"{where}: {path} is not readable")
            return
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            report.warn(f"{where}: {path} is readable by group or others (use mode 0600)")
    try:
        resolve_secret(ref)
    except SecretError as e:
        report.err(f"{where}: {e}")


def _action_ok(cfg: dict, action: Any) -> str | None:
    """Error text for a bad destination name, else None."""
    if not isinstance(action, str) or not action:
        return "must be a non-empty string"
    if action in RESERVED_DESTINATIONS:
        return f"{action!r} is reserved"
    if destination_type(cfg, action) is None:
        return f"unknown destination {action!r} (use hermes:<route>, script:<name> or a [destinations] table)"
    if action.startswith(("hermes:", "script:")) and not action.split(":", 1)[1]:
        return f"{action!r} has an empty name"
    return None


def _load_subscriptions(path: str) -> dict:
    data = json.loads(read_regular_text(path, "utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


def check(cfg: dict, *, load_plugin: Callable[[str, str | None], Any] | None = default_plugin_loader,
          check_secrets: bool = True) -> Report:
    """Validate a loaded config (ARCHITECTURE §15). Pure: reads files, writes nothing.

    ``load_plugin(name, module)`` returns the source plugin module (attributes used:
    ``auth_headers``, ``keep_headers``, ``secrets``, ``span``, ``action``,
    ``config_keys``, ``sender_retries``); pass None to skip plugin checks.
    """
    rep = Report()
    _unknown(rep, "", cfg, _TOP_KEYS)
    for sec, keys in _SECTION_KEYS.items():
        _unknown(rep, sec, cfg.get(sec, {}), keys)
    breaker = (cfg.get("dispatch") or {}).get("breaker", {})
    _unknown(rep, "dispatch.breaker", breaker, _BREAKER_KEYS)

    # numbers
    for sec, keys in (("intake", ("max_body_bytes", "busy_timeout_s", "threads")),
                      ("dispatch", ("parallel", "poll_s", "heartbeat_s", "lease_grace_s", "max_attempts",
                                    "backoff_base_s", "backoff_cap_s")),
                      ("limits", ("min_free_bytes",)),
                      ("retention", ("delivered_days", "attempts_days", "dropped_days", "issues_days")),
                      ("alerts", ("rate_limit_s", "timeout_s")),
                      ("hermes", ("timeout_s", "max_body_bytes"))):
        for k in keys:
            v = (cfg.get(sec) or {}).get(k)
            if v is not None:
                _num(rep, f"{sec}.{k}", v)
    for k in ("parallel", "max_attempts"):
        v = (cfg.get("dispatch") or {}).get(k)
        if isinstance(v, int) and not isinstance(v, bool) and v < 1:
            rep.err(f"dispatch.{k}: must be >= 1")
    grace = (cfg.get("dispatch") or {}).get("lease_grace_s")
    if isinstance(grace, (int, float)) and not isinstance(grace, bool) and grace < LEASE_MARGIN_S:
        # the worker deadline is lease_until - 5 s: below that, short timeouts get no time at all
        rep.err(f"dispatch.lease_grace_s: must be >= {LEASE_MARGIN_S:g} (a worker gives up {LEASE_MARGIN_S:g} s"
                " before its lease ends, so a smaller grace cuts every attempt short of its timeout)")
    if isinstance(breaker, dict):
        for k in _BREAKER_KEYS:
            if breaker.get(k) is not None:
                _num(rep, f"dispatch.breaker.{k}", breaker[k])
    co = cfg.get("coalesce") or {}
    for f in WINDOW_FIELDS:
        if co.get(f) is not None:
            _num(rep, f"coalesce.{f}", co[f], integer=f in ("max_events", "max_bytes"),
                 minimum=1 if f in ("max_events", "max_bytes") else 0)
    argv = (cfg.get("alerts") or {}).get("argv")
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) and a for a in argv)):
        rep.err("alerts.argv: must be a non-empty list of strings")

    # spans
    spans = cfg.get("spans")
    if not isinstance(spans, dict):
        rep.err("spans: must be a table")
        spans = {}
    for name, sp in spans.items():
        _unknown(rep, f"spans.{name}", sp, ("quiet_s", "max_s"))
        if isinstance(sp, dict):
            for f in ("quiet_s", "max_s"):
                if f not in sp:
                    rep.err(f"spans.{name}.{f}: required")
                else:
                    _num(rep, f"spans.{name}.{f}", sp[f])

    # sources
    sources = cfg.get("sources")
    if not isinstance(sources, dict):
        rep.err("sources: must be a table")
        sources = {}
    plugins: dict[str, Any] = {}
    delivered_s = float((cfg.get("retention") or {}).get("delivered_days") or 0) * DAY
    for name, s in sources.items():
        where = f"sources.{name}"
        if not isinstance(s, dict):
            rep.err(f"{where}: must be a table")
            continue
        plugin = None
        unbound = None
        if load_plugin is not None:
            try:
                plugin = unbound = load_plugin(name, s.get("module"))
            except Exception as e:  # noqa: BLE001 - any import failure is a config error
                rep.err(f"{where}: cannot import plugin {s.get('module') or 'hookspool.sources.' + name}:"
                        f" {e.__class__.__name__}")
            if plugin is not None:
                try:
                    plugin = bind_plugin(plugin, name, s)
                except Exception as e:  # noqa: BLE001 - a configurable plugin refused its config
                    msgs = getattr(e, "errors", None) or [e.__class__.__name__]
                    for m in msgs:
                        rep.err(f"{where}: {m}")
                    plugin = None
        plugins[name] = plugin
        known = plugin if plugin is not None else unbound
        extra = set(getattr(known, "config_keys", ()) or ()) if known is not None else set()
        for w in (getattr(plugin, "config_warnings", ()) or ()) if plugin is not None else ():
            rep.warn(f"{where}: {w}")
        _unknown(rep, where, s, SOURCE_KEYS | extra)
        if s.get("span") is not None and s["span"] not in spans:
            rep.err(f"{where}.span: unknown span preset {s['span']!r}")
        if plugin is not None and getattr(plugin, "span", None) and s.get("span") is None \
                and plugin.span not in spans:
            rep.err(f"{where}: plugin span {plugin.span!r} is not a defined preset")
        for f in WINDOW_FIELDS:
            if s.get(f) is not None:
                _num(rep, f"{where}.{f}", s[f], integer=f in ("max_events", "max_bytes"),
                     minimum=1 if f in ("max_events", "max_bytes") else 0)
        for f in ("dedup_window_s", "expect_every_s", "max_body_bytes"):
            if s.get(f) is not None:
                _num(rep, f"{where}.{f}", s[f])
        dw = dedup_window_s(cfg, name)
        if delivered_s and dw > delivered_s:
            rep.warn(f"{where}.dedup_window_s: {dw:g} s exceeds retention.delivered_days; a duplicate of a"
                     " pruned event is still answered 'duplicate'")
        if s.get("action") is not None:
            e = _action_ok(cfg, s["action"])
            if e:
                rep.err(f"{where}.action: {e}")
        elif plugin is not None and not getattr(plugin, "action", None):
            rep.err(f"{where}: no action in config and the plugin declares none")
        # secrets
        sec = s.get("secrets", {})
        if not isinstance(sec, dict):
            rep.err(f"{where}.secrets: must be a table of secret references")
            sec = {}
        for sname in (getattr(plugin, "secrets", ()) or ()) if plugin is not None else ():
            if sname not in sec:
                rep.err(f"{where}.secrets.{sname}: required by the plugin")
        if check_secrets:
            for sname, ref in sec.items():
                _check_secret(rep, f"{where}.secrets.{sname}", ref)
        # headers
        auth = {h.lower() for h in (getattr(plugin, "auth_headers", ()) or ())} if plugin is not None else set()
        keep = s.get("keep_headers")
        if keep is not None and not (isinstance(keep, list) and all(isinstance(h, str) for h in keep)):
            rep.err(f"{where}.keep_headers: must be a list of header names")
            keep = None
        keep_all = list(keep if keep is not None else (getattr(plugin, "keep_headers", ()) or ()))
        for h in keep_all:
            if h.lower() in auth:
                rep.err(f"{where}.keep_headers: {h} is an auth header and is never stored")
        if s.get("event_header") is not None:
            from .destinations.hermes import event_header_ok     # lazy: destinations import this module
            if not event_header_ok(s["event_header"]):
                rep.err(f"{where}.event_header: must be a plain header name (letters, digits, '-')"
                        " that hookspool does not set itself")
        if plugin is not None and getattr(plugin, "sender_retries", None) is False:
            rep.warn(f"{where}: the sender does not retry failed deliveries; its sweep must redeliver")

    # routes
    routes = cfg.get("routes")
    if not isinstance(routes, list):
        rep.err("routes: must be an array of tables ([[routes]])")
        routes = []
    names: set[str] = set()
    for i, r in enumerate(routes):
        where = f"routes[{i}]"
        if not isinstance(r, dict):
            rep.err(f"{where}: must be a table")
            continue
        _unknown(rep, where, r, ROUTE_KEYS)
        n = r.get("name")
        if not isinstance(n, str) or not n:
            rep.err(f"{where}.name: required")
        else:
            where = f"routes.{n}"
            if n in names:
                rep.err(f"{where}: duplicate route name")
            if n in RESERVED_DESTINATIONS:
                rep.err(f"{where}: {n!r} is reserved")
            names.add(n)
        if r.get("source") not in sources:
            rep.err(f"{where}.source: {r.get('source')!r} is not a configured source")
        m = r.get("match", {})
        if not isinstance(m, dict) or any(isinstance(v, (dict, list)) for v in m.values()):
            rep.err(f"{where}.match: must be a table of scalar values")
        if r.get("destination") is not None:
            e = _action_ok(cfg, r["destination"])
            if e:
                rep.err(f"{where}.destination: {e}")
        for f in WINDOW_FIELDS:
            if r.get(f) is not None:
                _num(rep, f"{where}.{f}", r[f], integer=f in ("max_events", "max_bytes"),
                     minimum=1 if f in ("max_events", "max_bytes") else 0)

    # destinations
    dests = cfg.get("destinations")
    if not isinstance(dests, dict):
        rep.err("destinations: must be a table")
        dests = {}
    for name, d in dests.items():
        where = f"destinations.{name}"
        if name in RESERVED_DESTINATIONS:
            rep.err(f"{where}: {name!r} is reserved")
            continue
        if not isinstance(d, dict):
            rep.err(f"{where}: must be a table")
            continue
        t = destination_type(cfg, name)
        if t is None:
            rep.err(f"{where}.type: required for a destination not named hermes:<route> or script:<name>")
            continue
        if t != "module" and t not in DEST_KEYS:
            rep.err(f"{where}.type: unknown destination type {t!r} (hermes, script, http, or module = ...)")
            continue
        if t != "module":
            _unknown(rep, where, d, DEST_KEYS[t])
        for f in ("timeout_s", "max_body_bytes", "max_inflight"):
            if d.get(f) is not None:
                _num(rep, f"{where}.{f}", d[f], minimum=1 if f != "timeout_s" else 0)
        if t == "script":
            a = d.get("argv")
            if not (isinstance(a, list) and a and all(isinstance(x, str) and x for x in a)):
                rep.err(f"{where}.argv: must be a non-empty list of strings")
            for f in ("cwd", "state_dir", "env_file"):
                if d.get(f) is not None and not (isinstance(d[f], str) and os.path.isabs(os.path.expanduser(d[f]))):
                    rep.err(f"{where}.{f}: must be an absolute path")
        if t == "http":
            url = d.get("url")
            if not isinstance(url, str) or "://" not in url:
                rep.err(f"{where}.url: required")
            elif not url.startswith("https://") and not d.get("allow_insecure"):
                rep.err(f"{where}.url: https:// required unless allow_insecure = true")
            if d.get("secret") is None:
                rep.err(f"{where}.secret: required")
            elif check_secrets:
                _check_secret(rep, f"{where}.secret", d["secret"])
            if d.get("secret_previous") is not None and check_secrets:
                _check_secret(rep, f"{where}.secret_previous", d["secret_previous"])
            h = d.get("headers", {})
            if not isinstance(h, dict) or not all(isinstance(v, str) for v in h.values()):
                rep.err(f"{where}.headers: must be a table of strings")
    for name in dests:
        if name.startswith("script:") and not dests[name].get("argv"):
            pass  # reported above

    # feeds: every (source, route, destination) the config can produce
    feeds = list(_feeds(cfg, sources, plugins))
    used_dests = {d for _, _, d in feeds if d}
    for d in sorted(used_dests):
        if d.startswith("script:") and d not in dests:
            rep.err(f"destination {d}: no [destinations.\"{d}\"] table with argv")
    for src, route, dest in feeds:
        w = window_for(cfg, src, route if route != src else None,
                       plugin_span=getattr(plugins.get(src), "span", None))
        where = f"route {route}" + (f" (source {src})" if route != src else "")
        if w["quiet_s"] > w["max_s"]:
            rep.warn(f"{where}: quiet_s {w['quiet_s']:g} > max_s {w['max_s']:g}; the quiet window can never fire")
        cap = destination_max_body_bytes(cfg, dest) if dest else None
        if cap is not None and w["max_bytes"] > 0.75 * cap:
            rep.err(f"{where}: max_bytes {w['max_bytes']} exceeds 0.75 x {dest} max_body_bytes ({cap})")
        smax = source_cfg(cfg, src).get("max_body_bytes")
        if smax is not None and isinstance(smax, int) and smax > w["max_bytes"]:
            rep.err(f"{where}: source max_body_bytes {smax} exceeds the route's max_bytes {w['max_bytes']};"
                    " one event could overflow the document")

    # Hermes routes fed by hookspool must not filter or coalesce on the gateway side.
    hermes_routes = sorted({d.split(":", 1)[1] for d in used_dests if destination_type(cfg, d) == "hermes"})
    if hermes_routes:
        sp = (cfg.get("hermes") or {}).get("subscriptions_path")
        try:
            subs = _load_subscriptions(sp)
        except (OSError, ValueError, TypeError) as e:
            rep.err(f"hermes.subscriptions_path: cannot read {sp}: {e.__class__.__name__}")
            subs = None
        if subs is not None:
            for hr in hermes_routes:
                sub = subs.get(hr)
                if not isinstance(sub, dict):
                    rep.warn(f"hermes:{hr}: no such route in {sp} (deliveries will retry as config errors)")
                    continue
                if sub.get("events"):
                    rep.err(f"hermes:{hr}: the gateway route sets 'events'; it filters whole batches on one"
                            " event type. Remove it and route by attrs in hookspool")
                if sub.get("coalesce"):
                    rep.err(f"hermes:{hr}: the gateway route sets 'coalesce' (in memory, keeps the last event"
                            " only). Remove it; hookspool coalesces")
    return rep


def _feeds(cfg: dict, sources: dict, plugins: dict):
    """(source, route, destination) for every configured route and every implicit route."""
    for r in cfg.get("routes") or []:
        if isinstance(r, dict) and r.get("source") in sources and r.get("name"):
            src = r["source"]
            dest = r.get("destination") or sources[src].get("action") or getattr(plugins.get(src), "action", None)
            yield src, r["name"], dest if isinstance(dest, str) else None
    for src, s in sources.items():
        if not isinstance(s, dict):
            continue
        dest = s.get("action") or getattr(plugins.get(src), "action", None)
        yield src, src, dest if isinstance(dest, str) else None
