"""The processor runtime for service packages (ARCHITECTURE 14.3.4) and the batch protocol of
``script:`` destinations (6.6).

``python -m hookspool.processor <instance>`` is what the dispatcher runs for a service package that has
a processor. It reads the batch document on stdin, loads ``processor.py`` from
``$HOOKSPOOL_SERVICE_DIR`` and calls::

    def process(batch: Batch, ctx: Context) -> list[dict]:
        '''Return items; each is {"event_type": ..., <payload fields>}; [] means nothing to do.'''

The environment (built by the config expansion, 14.3.3): ``HOOKSPOOL_SERVICE_PACKAGE``,
``HOOKSPOOL_SERVICE_INSTANCE``, ``HOOKSPOOL_SERVICE_DIR``, ``HOOKSPOOL_SETTINGS`` (JSON, typed, defaults
applied), ``HOOKSPOOL_DELTA_ROUTES`` (JSON, event type -> destination), ``HOOKSPOOL_SECRET_REFS`` (JSON,
secret name -> reference, never a value), and from the ``script`` destination ``HOOKSPOOL_BATCH_ID``,
``HOOKSPOOL_DELIVERY_ID`` and, when the package keeps state, ``HOOKSPOOL_STATE_DIR``.

One batch, with the state lock (``<state_dir>/.lock``) held throughout:

1. if ``<state_dir>/journal/<batch id>.json`` exists, it is replayed and ``process`` is not called;
2. otherwise ``process`` runs; ``ctx.save_state`` only STAGES its writes; the returned items become
   ``{"enqueue": ...}`` emits addressed through ``HOOKSPOOL_DELTA_ROUTES`` (an item whose event type the
   deployment did not map is dropped, with one log line per batch); the emits and the staged writes are
   written to the journal (fsync, atomic rename);
3. the staged writes are applied (a state file that changed since the pass read it is left alone, so an
   old journal can never move state backwards), the emits are printed, exit 0.

``Permanent`` (also raised by ``ctx.fail_permanent``) exits 65, the dispatcher's permanent class. Its
reason IS logged (whitespace collapsed, first 300 characters) and the script destination keeps the tail
of the standard error as the attempt's error, so the reason is persisted in the spool and shown by
``show``: pass a short fixed phrase, never a response body or a secret. The text of a chained exception
is never logged. ``Retryable`` or any other exception exits 75 (retry), with only the exception class in
the log. Nothing but emit lines ever reaches stdout: while the runtime runs, file descriptor 1 is the
standard error.

Limits worth knowing. A package that keeps no state (no ``HOOKSPOOL_STATE_DIR``) has no journal, so a
retried batch calls ``process`` again and the stable emit dedup id is the only guard against duplicates.
The runtime trusts ``HOOKSPOOL_SERVICE_DIR`` from the dispatcher (``hookspool check`` verifies the
directory; only the ``hooks`` file is path-checked here, 14.3.6). Settings that the manifest does not
declare pass through unchanged, and ``hooks`` loads even when ``[settings.hooks]`` is not declared:
the config expansion and ``check`` are the validators, not this runtime.

The module also keeps the helpers that the stand-alone delta scripts in ``deploy/scripts`` use
(``read_document``, ``make_emit``, ``save``/``staging``, ``run_batch``, the journal);
``deploy/scripts/hookspool_batch.py`` is a re-export of this module.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import importlib
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
import time
import tomllib
from collections.abc import Mapping
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, NoReturn

try:                                     # the network layer defines the classes ctx.http raises
    from .net import Permanent, Retryable
except ImportError:                      # pragma: no cover - only before the network module exists
    class Retryable(Exception):          # type: ignore[no-redef]
        """A failure that may pass on a later attempt (exit 75)."""

    class Permanent(Exception):          # type: ignore[no-redef]
        """A failure no retry can fix (exit 65)."""

JOURNAL_KEEP_S = 14 * 86400
EX_OK, EX_PERMANENT, EX_RETRY = 0, 65, 75
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
_STATE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_MODULE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
EMIT_KEYS = ("source", "key", "action", "payload", "route", "event_type", "dedup_id", "quiet_s", "max_s",
             "max_events", "max_bytes", "not_before")
SETTING_TYPES = {"string": (str,), "path": (str,), "int": (int,), "bool": (bool,), "list": (list,)}
LOCK_FILE, RUNTIME_FILE = ".lock", ".runtime.json"

__all__ = ["Batch", "Context", "Settings", "Retryable", "Permanent", "BatchError", "run", "main",
           "EX_OK", "EX_PERMANENT", "EX_RETRY"]


class BatchError(RuntimeError):
    """The batch cannot be processed (bad document, no state directory, bad environment)."""


# ---- the document ------------------------------------------------------------------------------------
def read_document(stream=None) -> dict:
    raw = (stream or sys.stdin).read()
    try:
        doc = json.loads(raw)
    except ValueError:
        raise BatchError("stdin is not a hookspool batch document") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("events"), list):
        raise BatchError("stdin is not a hookspool batch document (no events list)")
    return doc


def events(doc: dict) -> list[dict]:
    """The payload objects of the batch, oldest first (non-object entries are skipped)."""
    return [e for e in doc["events"] if isinstance(e, dict)]


def batch_id(doc: dict | None = None, *, env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    v = env.get("HOOKSPOOL_BATCH_ID") or (str((doc or {}).get("batch_id") or ""))
    if not v:
        raise BatchError("no HOOKSPOOL_BATCH_ID")
    return v


def delivery_id(doc: dict | None = None, *, env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    return env.get("HOOKSPOOL_DELIVERY_ID") or (str((doc or {}).get("delivery_id") or "")) or batch_id(doc, env=env)


def state_dir(default: str = "") -> str:
    d = os.environ.get("HOOKSPOOL_STATE_DIR") or default
    if not d:
        raise BatchError("no state directory: set state_dir on the destination (HOOKSPOOL_STATE_DIR)")
    return d


# ---- emits ---------------------------------------------------------------------------------------------
def destinations(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """``event_type -> destination`` from ``$HOOKSPOOL_DELTA_ROUTES`` (a JSON object), e.g.
    ``{"conflict": "hermes:calendar"}``."""
    raw = (os.environ if env is None else env).get("HOOKSPOOL_DELTA_ROUTES", "")
    if not raw.strip():
        return {}
    try:
        m = json.loads(raw)
    except ValueError:
        raise BatchError("HOOKSPOOL_DELTA_ROUTES is not JSON") from None
    if not isinstance(m, dict) or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in m.items()):
        raise BatchError("HOOKSPOOL_DELTA_ROUTES must map event types to destination names")
    return m


def make_emit(source: str, key: str, item: dict, index: int, did: str, routes: dict[str, str], *,
              default_prefix: str = "hermes:") -> dict:
    """One ``enqueue`` object for a result item. The destination is ``routes[event_type]`` or
    ``<default_prefix><event_type>``. Emits are results that are already coalesced, so they are not
    delayed (``quiet_s = max_s = 0``). The dedup id is per delivery id and position, so a replay of
    this batch can never enqueue the same result twice."""
    et = str(item.get("event_type") or "")
    if not et:
        raise BatchError("a result has no event_type")
    action = routes.get(et) or f"{default_prefix}{et}"
    route = action.split(":", 1)[1] if ":" in action else action
    return {"source": source, "key": str(key)[:200] or "x", "action": action, "route": route or et, "payload": item,
            "event_type": et, "dedup_id": f"{did}:{index}", "quiet_s": 0, "max_s": 0}


def format_emit(e: dict) -> str:
    bad = sorted(set(e) - set(EMIT_KEYS))
    if bad:
        raise BatchError(f"emit has unknown keys {bad}")
    return json.dumps({"enqueue": e}, separators=(",", ":"), ensure_ascii=True, sort_keys=True)


# ---- staging state writes ----------------------------------------------------------------------------------
_staged: dict[str, Any] | None = None
_pre: dict[str, str] = {}                  # path -> digest of the file as it was when the pass first staged it


@contextmanager
def staging() -> Iterator[dict[str, Any]]:
    """While active, ``save`` records writes here instead of making them."""
    global _staged, _pre
    prev, prev_pre, _staged, _pre = _staged, _pre, {}, {}
    try:
        yield _staged
    finally:
        _staged, _pre = prev, prev_pre


def _digest(path: str) -> str:
    """Content digest of ``path`` ("" when it does not exist): the guard against replaying a stale journal."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


def save(path: str, obj: Any, writer: Callable[[str, Any], None]) -> None:
    """Write ``obj`` to ``path`` with ``writer``, or stage it while ``staging()`` is active."""
    if _staged is None:
        writer(path, obj)
    else:
        _pre.setdefault(path, _digest(path))
        _staged[path] = copy.deepcopy(obj)


def staged(path: str, default: Any = None) -> Any:
    """What ``save`` staged for ``path`` (so a script reads its own writes), else ``default``."""
    return default if _staged is None or path not in _staged else copy.deepcopy(_staged[path])


# ---- the journal --------------------------------------------------------------------------------------------------
def _journal_path(sdir: str, bid: str) -> str:
    return os.path.join(sdir, "journal", _UNSAFE.sub("_", bid) + ".json")


def _atomic_write(path: str, obj: Any) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:                                                       # make the rename itself durable
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def load_journal(sdir: str, bid: str) -> dict | None:
    try:
        with open(_journal_path(sdir, bid), encoding="utf-8") as f:
            j = json.load(f)
    except (OSError, ValueError):
        return None
    return j if isinstance(j, dict) and isinstance(j.get("emits"), list) and isinstance(j.get("state"), dict) else None


def prune_journal(sdir: str, now: float | None = None, keep_s: float = JOURNAL_KEEP_S) -> int:
    d, n, now = os.path.join(sdir, "journal"), 0, time.time() if now is None else now
    try:
        names = os.listdir(d)
    except OSError:
        return 0
    for name in names:
        p = os.path.join(d, name)
        try:
            if now - os.stat(p).st_mtime > keep_s:
                os.unlink(p)
                n += 1
        except OSError:
            pass
    return n


def _new_journal(bid: str, emits: list[dict], st: dict[str, Any]) -> dict:
    return {"batch": bid, "emits": [json.loads(format_emit(e))["enqueue"] for e in emits],
            "state": dict(st), "pre": dict(_pre), "applied": False}


def _apply(sdir: str, bid: str, j: dict, writer: Callable[[str, Any], None], err, who: str) -> None:
    """Apply a journal's staged writes once (see the digest guard below) and mark it applied."""
    if j.get("applied"):
        return
    pre = j.get("pre") if isinstance(j.get("pre"), dict) else {}
    for path, obj in j["state"].items():
        if path in pre and _digest(path) != pre[path]:
            # The file is no longer what this pass read: either this journal was already (partly) applied
            # before a crash, or a later batch for the same key moved it on. Writing the older state over
            # a newer one would make the next pass report the same changes again, so leave it alone.
            err.write(f"{who}: not applying stale state to {os.path.basename(path)}\n")
            continue
        writer(path, obj)
    j["applied"] = True
    _atomic_write(_journal_path(sdir, bid), j)


def _print_emits(emits: list[dict], out) -> None:
    for e in emits:
        out.write(format_emit(e) + "\n")
    out.flush()


def run_batch(doc: dict, work: Callable[[], list[dict]], writer: Callable[[str, Any], None], *,
              default_state_dir: str = "", out=None, now: float | None = None) -> int:
    """Run one batch for a stand-alone script. ``work()`` returns the list of ``enqueue`` objects (see
    ``make_emit``) and makes its state writes through ``save`` (staged here). Returns the exit code."""
    out = out or sys.stdout
    sdir, bid = state_dir(default_state_dir), batch_id(doc)
    j = load_journal(sdir, bid)
    if j is None:
        with staging() as st:
            emits = work()
            j = _new_journal(bid, emits, st)
        _atomic_write(_journal_path(sdir, bid), j)
    _apply(sdir, bid, j, writer, sys.stderr, "hookspool_batch")
    _print_emits(j["emits"], out)
    prune_journal(sdir, now)
    return 0


# ---- the processor runtime (14.3.4) -------------------------------------------------------------------------------
@dataclass(frozen=True)
class Batch:
    """One delivered batch: ``events`` are the stored payloads, oldest first; ``doc`` is the whole document."""
    id: str
    delivery_id: str
    source: str
    key: str
    events: list[dict]
    doc: dict = field(repr=False)

    @classmethod
    def from_doc(cls, doc: dict, env: Mapping[str, str] | None = None) -> "Batch":
        return cls(id=batch_id(doc, env=env), delivery_id=delivery_id(doc, env=env),
                   source=str(doc.get("source") or ""), key=str(doc.get("key") or ""), events=events(doc), doc=doc)


class Settings(Mapping):
    """The instance's typed settings (read only); ``ctx.settings["tz"]`` or ``ctx.settings.tz``."""

    def __init__(self, values: Mapping[str, Any]):
        object.__setattr__(self, "_v", dict(values))

    def __getitem__(self, name: str) -> Any:
        return copy.deepcopy(self._v[name])

    def __iter__(self) -> Iterator[str]:
        return iter(self._v)

    def __len__(self) -> int:
        return len(self._v)

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("settings are read only")

    def __repr__(self) -> str:
        return f"<Settings {sorted(self._v)}>"


def read_manifest(service_dir: str) -> dict:
    """``service.toml`` of the package ({} when there is none)."""
    p = os.path.join(service_dir, "service.toml")
    try:
        with open(p, "rb") as f:
            m = tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise BatchError(f"service.toml cannot be read ({e.__class__.__name__})") from None
    return m if isinstance(m, dict) else {}


def _type_ok(kind: str, value: Any) -> bool:
    types = SETTING_TYPES.get(kind)
    if types is None:
        return False
    if kind == "int" and isinstance(value, bool):
        return False
    return isinstance(value, types)


def load_settings(env: Mapping[str, str], manifest: Mapping[str, Any] | None = None) -> Settings:
    """``$HOOKSPOOL_SETTINGS`` over the manifest's defaults, each checked against its declared type."""
    raw = env.get("HOOKSPOOL_SETTINGS", "")
    try:
        values = json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise BatchError("HOOKSPOOL_SETTINGS is not JSON") from None
    if not isinstance(values, dict):
        raise BatchError("HOOKSPOOL_SETTINGS must be a JSON object")
    specs = (manifest or {}).get("settings") or {}
    for name, spec in specs.items() if isinstance(specs, dict) else ():
        if not isinstance(spec, dict):
            continue
        if name not in values and "default" in spec:
            values[name] = copy.deepcopy(spec["default"])
        kind = spec.get("type")
        if name in values and isinstance(kind, str) and not _type_ok(kind, values[name]):
            raise BatchError(f"setting {name} is not of type {kind}")
    return Settings(values)


def _secret_refs(env: Mapping[str, str]) -> dict[str, Any]:
    raw = env.get("HOOKSPOOL_SECRET_REFS", "")
    try:
        refs = json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise BatchError("HOOKSPOOL_SECRET_REFS is not JSON") from None
    if not isinstance(refs, dict):
        raise BatchError("HOOKSPOOL_SECRET_REFS must map secret names to references")
    return refs


def _code_path_ok(path: str) -> bool:
    """14.3.6: an absolute path to a regular file (not a symlink), not group/other writable, owned by root
    or us."""
    if not os.path.isabs(path):
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) \
        and st.st_uid in (0, os.getuid())


def _private_name(kind: str, instance: str) -> str:
    return f"hookspool._{kind}_{re.sub(r'[^A-Za-z0-9_]', '_', instance)}"


def _load_file(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise BatchError(f"cannot load {os.path.basename(path)}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return mod


def load_processor(service_dir: str, instance: str, manifest: Mapping[str, Any] | None = None):
    """The package's processor module: imported by name for a built-in package, by file path (under a
    private module name, never on ``sys.path``) for one in a ``packages.path`` directory."""
    if not service_dir or not os.path.isdir(service_dir):
        raise BatchError("HOOKSPOOL_SERVICE_DIR is not a directory")
    proc = (manifest or {}).get("processor")
    module = (proc.get("module") if isinstance(proc, dict) else None) or "processor"
    if not isinstance(module, str) or not _MODULE_NAME.match(module):
        raise BatchError("the manifest's processor module must be a module name in the package")
    real = os.path.realpath(service_dir)
    builtin = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "services"))
    if os.path.dirname(real) == builtin and _MODULE_NAME.match(os.path.basename(real)):
        mod = importlib.import_module(f"hookspool.services.{os.path.basename(real)}.{module}")
    else:
        path = os.path.join(real, module + ".py")
        if not os.path.isfile(path):
            raise BatchError(f"the package has no {module}.py")
        mod = _load_file(_private_name("processor", instance), path)
    if not callable(getattr(mod, "process", None)):
        raise BatchError(f"{module}.py defines no process(batch, ctx)")
    return mod


def load_hooks(settings: Settings, instance: str):
    """The deployment's hooks module named by the ``hooks`` setting (14.3.6), or None."""
    path = settings.get("hooks") if "hooks" in settings else None
    if not path:
        return None
    if not isinstance(path, str) or not _code_path_ok(path):
        raise BatchError("the hooks setting must be an absolute path to a regular file that is not group- or "
                         "other-writable, owned by root or this user")
    return _load_file(_private_name("hooks", instance), path)


def _default_extras(state: dict, secret_resolver: Callable[[str], str], **kw: Any) -> Mapping[str, Any]:
    """The network layer's ``{"http", "oauth", "secret"}``; ``kw`` is its ``log`` / ``now`` / ``persist``."""
    from .net import make_context_extras            # imported only when a processor uses ctx.http / ctx.oauth
    return make_context_extras(state, secret_resolver, **kw)


class Context:
    """What ``process`` gets besides the batch (14.3.4). A package never reads the environment, opens
    files, logs or sleeps on its own; it goes through this object."""

    def __init__(self, *, instance: str, settings: Settings | None = None, secret_refs: Mapping[str, Any] | None = None,
                 state_dir: str = "", hooks: Any = None, runtime_state: dict | None = None, err=None,
                 clock: Callable[[], float] = time.time,
                 extras_factory: Callable[[dict, Callable[[str], str]], Mapping[str, Any]] | None = None,
                 persist: Callable[[], None] | None = None):
        from .plugin import Secrets
        self.instance = instance
        self.settings = settings if settings is not None else Settings({})
        self.hooks = hooks
        self._refs = dict(secret_refs or {})
        self._secrets = Secrets(self._refs, source=instance)
        self._sdir = state_dir
        self._runtime = runtime_state if runtime_state is not None else {}
        self._err = err
        self._clock = clock
        self._factory = extras_factory
        self._persist = persist
        self._extras: Mapping[str, Any] | None = None

    # secrets and the network ------------------------------------------------------------------
    def _resolve(self, name: str) -> str:
        """The resolver handed to the network layer: ``KeyError`` for a secret this processor was not
        given (which it turns into ``Permanent``), ``SecretError`` for a reference that cannot be read
        now (``Retryable``)."""
        if name not in self._refs:
            raise KeyError(name)
        return self._secrets[name]

    def secret(self, name: str) -> str:
        """The value of a ``processor = true`` secret, resolved now. Never log it. A name the processor
        was not given is ``Permanent``; a reference that cannot be read now is ``Retryable``."""
        access = self._x().get("secret")
        return access(name) if access is not None else self._resolve(name)

    def _x(self) -> Mapping[str, Any]:
        if self._extras is None:
            if self._factory is None:
                self._extras = _default_extras(self._runtime, self._resolve, log=self.log, now=self._clock,
                                               persist=self._persist)
            else:
                self._extras = self._factory(self._runtime, self._resolve)
        return self._extras

    @property
    def http(self) -> Any:
        return self._x()["http"]

    def oauth(self, secret_name: str) -> Any:
        return self._x()["oauth"](secret_name)

    # state ------------------------------------------------------------------------------------------
    def _state_path(self, name: str) -> str:
        if not isinstance(name, str) or not _STATE_NAME.match(name):
            raise BatchError("a state name is letters, digits, '_', '-' and '.' (at most 100)")
        if not self._sdir:
            raise BatchError("no state directory: the package's manifest needs [processor] state = true")
        return os.path.join(self._sdir, name + ".json")

    def load_state(self, name: str, default: Any = None) -> Any:
        """The state saved under ``name`` (what this pass staged, else the file), or ``default``."""
        path = self._state_path(name)
        if _staged is not None and path in _staged:
            return copy.deepcopy(_staged[path])
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return copy.deepcopy(default)
        except (OSError, ValueError) as e:
            raise BatchError(f"state {name} cannot be read ({e.__class__.__name__})") from None

    def save_state(self, name: str, obj: Any) -> None:
        """Stage ``obj`` as the state ``name``; it is written only after the journal records the result."""
        path = self._state_path(name)
        json.dumps(obj)                              # not JSON: fail now, inside process
        save(path, obj, _atomic_write)

    # the rest ---------------------------------------------------------------------------------------------
    def log(self, line: Any) -> None:
        """One line to stderr. Ids and counts only, never bodies or secrets."""
        text = " ".join(str(line).split())[:1000]
        err = self._err or sys.stderr
        err.write(f"hookspool.processor {self.instance}: {text}\n")
        err.flush()

    def now(self) -> float:
        return self._clock()

    def fail_permanent(self, reason: str) -> NoReturn:
        """Stop for good (exit 65). The reason is logged and persisted with the attempt: a short fixed
        phrase, never a response body or a secret."""
        raise Permanent(reason)


def build_emits(items: Any, batch: Batch, instance: str, routes: Mapping[str, str], log: Callable[[str], None]) -> list[dict]:
    """``enqueue`` objects for the items ``process`` returned. Unmapped event types are dropped."""
    if not isinstance(items, list):
        raise BatchError("process must return a list of items")
    emits: list[dict] = []
    dropped: dict[str, int] = {}
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise BatchError("an item is not an object")
        et = item.get("event_type")
        if not isinstance(et, str) or not et:
            raise BatchError("an item has no event_type")
        if et not in routes:
            dropped[et] = dropped.get(et, 0) + 1
            continue
        key = item.get("key")
        key = str(key) if key not in (None, "") else batch.key
        emits.append(make_emit(instance, key, item, i, batch.delivery_id, dict(routes)))
    if dropped:
        log(f"batch {batch.id}: dropped {sum(dropped.values())} item(s) of unmapped event types: "
            + ", ".join(f"{t} x{n}" for t, n in sorted(dropped.items())))
    return emits


@contextmanager
def _state_lock(sdir: str) -> Iterator[None]:
    if not sdir:
        yield
        return
    os.makedirs(sdir, mode=0o700, exist_ok=True)
    fd = os.open(os.path.join(sdir, LOCK_FILE), os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _load_runtime(sdir: str) -> dict:
    if not sdir:
        return {}
    try:
        with open(os.path.join(sdir, RUNTIME_FILE), encoding="utf-8") as f:
            v = json.load(f)
    except (OSError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}


def run(instance: str, doc: dict | None = None, *, env: Mapping[str, str] | None = None, stdin=None, out=None,
        err=None, clock: Callable[[], float] = time.time,
        extras_factory: Callable[[dict, Callable[[str], str]], Mapping[str, Any]] | None = None) -> int:
    """Run one batch through the instance's processor; returns the exit code (0, 65 or 75)."""
    env = os.environ if env is None else env
    out, err = out or sys.stdout, err or sys.stderr

    def log(line: str) -> None:
        err.write(f"hookspool.processor {instance}: {line}\n")
        err.flush()
    try:
        if doc is None:
            doc = read_document(stdin or sys.stdin)
        batch = Batch.from_doc(doc, env)
        routes = destinations(env)
        sdir = env.get("HOOKSPOOL_STATE_DIR", "")
        service_dir = env.get("HOOKSPOOL_SERVICE_DIR", "")
        with _state_lock(sdir):
            j = load_journal(sdir, batch.id) if sdir else None
            if j is not None:
                log(f"batch {batch.id}: replaying the journal ({len(j['emits'])} emit(s))")
            else:
                runtime = _load_runtime(sdir)
                before = json.dumps(runtime, sort_keys=True, default=str)

                def persist() -> None:                # a rotated refresh token is written at once
                    if sdir:
                        _atomic_write(os.path.join(sdir, RUNTIME_FILE), runtime)
                try:
                    with redirect_stdout(err):
                        manifest = read_manifest(service_dir) if service_dir else {}
                        settings = load_settings(env, manifest)
                        mod = load_processor(service_dir, instance, manifest)
                        ctx = Context(instance=instance, settings=settings, secret_refs=_secret_refs(env),
                                      state_dir=sdir, hooks=load_hooks(settings, instance), runtime_state=runtime,
                                      err=err, clock=clock, extras_factory=extras_factory, persist=persist)
                        with staging() as st:
                            emits = build_emits(mod.process(batch, ctx), batch, instance, routes, log)
                            j = _new_journal(batch.id, emits, st)
                finally:
                    # Cached access tokens and a rotated refresh token are kept whatever the outcome.
                    if sdir and json.dumps(runtime, sort_keys=True, default=str) != before:
                        _atomic_write(os.path.join(sdir, RUNTIME_FILE), runtime)
                if sdir:
                    _atomic_write(_journal_path(sdir, batch.id), j)
            if sdir:
                _apply(sdir, batch.id, j, _atomic_write, err, f"hookspool.processor {instance}")
            _print_emits(j["emits"], out)
            if sdir:
                prune_journal(sdir)                   # file times are wall-clock times
        return EX_OK
    except Permanent as e:
        log(f"permanent failure ({e.__class__.__name__}): {' '.join(str(e).split())[:300]}")
        return EX_PERMANENT
    except (Exception, SystemExit) as e:
        log(f"failed ({e.__class__.__name__}); the batch will be retried")
        return EX_RETRY


def main(argv: list[str] | None = None) -> int:
    """``python -m hookspool.processor <instance>``. Standard output is the standard error while the
    runtime runs; only the emits go to the original standard output."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not args[0]:
        sys.stderr.write("usage: python -m hookspool.processor <instance>\n")
        return EX_RETRY
    instance = args[0]
    named = os.environ.get("HOOKSPOOL_SERVICE_INSTANCE")
    if named and named != instance:
        sys.stderr.write(f"hookspool.processor {instance}: HOOKSPOOL_SERVICE_INSTANCE names another instance\n")
        return EX_RETRY
    sys.stdout.flush()
    real = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    saved, sys.stdout = sys.stdout, sys.stderr
    try:
        return run(instance, stdin=sys.stdin, out=real, err=sys.stderr)
    finally:
        sys.stdout = saved
        try:
            real.close()
        except OSError:
            pass


if __name__ == "__main__":
    # Run the canonical module, not this __main__ copy, so a processor that imports Permanent or
    # Retryable from hookspool.processor raises the very classes run() catches.
    from hookspool import processor as _canonical
    sys.exit(_canonical.main())
