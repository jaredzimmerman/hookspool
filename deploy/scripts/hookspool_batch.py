"""Batch protocol for scripts run by a hookspool ``script:`` destination (ARCHITECTURE section 6.6).

The dispatcher runs the script with the batch document on stdin and ``HOOKSPOOL_BATCH_ID`` /
``HOOKSPOOL_DELIVERY_ID`` (stable for every attempt of one batch) and, when the destination sets
``state_dir``, ``HOOKSPOOL_STATE_DIR``. Exit 0 marks the batch delivered; stdout lines of the form
``{"enqueue": {...}}`` are committed to the spool in the same transaction as that mark. Exit non-zero
(or a timeout, or a lost lease) means the batch runs again, possibly several times.

A delta script reads some outside state, advances its own state (a Gmail history id, a Calendar sync
token) and reports what needs an agent. Run naively, a kill between "state advanced" and "emits
committed" loses the report: the retry finds nothing new. ``run_batch`` closes that gap with a
journal in the state directory, keyed by the batch id:

1. ``work()`` computes the emits and STAGES its state writes (``save`` records them in memory
   instead of touching the files);
2. the emits and the staged writes are written to ``<state>/journal/<batch>.json`` (fsync, atomic
   rename);
3. only then are the staged writes applied and the emits printed.

A retry of the same batch finds the journal and replays it without running ``work()`` again, so the
state advances once and the report is never lost. Entries older than ``JOURNAL_KEEP_S`` are removed
after a successful run. Stdlib only.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

JOURNAL_KEEP_S = 14 * 86400
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
EMIT_KEYS = ("source", "key", "action", "payload", "route", "event_type", "dedup_id", "quiet_s", "max_s",
             "max_events", "max_bytes", "not_before")


class BatchError(RuntimeError):
    """The batch cannot be processed (bad document, no state directory)."""


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


def batch_id(doc: dict | None = None) -> str:
    v = os.environ.get("HOOKSPOOL_BATCH_ID") or (str((doc or {}).get("batch_id") or ""))
    if not v:
        raise BatchError("no HOOKSPOOL_BATCH_ID")
    return v


def delivery_id(doc: dict | None = None) -> str:
    return os.environ.get("HOOKSPOOL_DELIVERY_ID") or (str((doc or {}).get("delivery_id") or "")) or batch_id(doc)


def state_dir(default: str = "") -> str:
    d = os.environ.get("HOOKSPOOL_STATE_DIR") or default
    if not d:
        raise BatchError("no state directory: set state_dir on the destination (HOOKSPOOL_STATE_DIR)")
    return d


# ---- emits ---------------------------------------------------------------------------------------------
def destinations() -> dict[str, str]:
    """``event_type -> destination`` from ``$HOOKSPOOL_DELTA_ROUTES`` (a JSON object, from the
    destination's env_file), e.g. ``{"conflict": "hermes:calendar"}``."""
    raw = os.environ.get("HOOKSPOOL_DELTA_ROUTES", "")
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


def run_batch(doc: dict, work: Callable[[], list[dict]], writer: Callable[[str, Any], None], *,
              default_state_dir: str = "", out=None, now: float | None = None) -> int:
    """Run one batch. ``work()`` returns the list of ``enqueue`` objects (see ``make_emit``) and makes its
    state writes through ``save`` (staged here). Returns the exit code."""
    out = out or sys.stdout
    sdir, bid = state_dir(default_state_dir), batch_id(doc)
    j = load_journal(sdir, bid)
    if j is None:
        with staging() as st:
            emits = work()
            j = {"batch": bid, "emits": [json.loads(format_emit(e))["enqueue"] for e in emits],
                 "state": dict(st), "pre": dict(_pre), "applied": False}
        _atomic_write(_journal_path(sdir, bid), j)
    if not j.get("applied"):
        pre = j.get("pre") if isinstance(j.get("pre"), dict) else {}
        for path, obj in j["state"].items():
            if path in pre and _digest(path) != pre[path]:
                # The file is no longer what this pass read: either this journal was already (partly) applied
                # before a crash, or a later batch for the same key moved it on. Writing the older state over
                # a newer one would make the next pass report the same changes again, so leave it alone.
                sys.stderr.write(f"hookspool_batch: not applying stale state to {os.path.basename(path)}\n")
                continue
            writer(path, obj)
        j["applied"] = True
        _atomic_write(_journal_path(sdir, bid), j)
    for e in j["emits"]:
        out.write(format_emit(e) + "\n")
    out.flush()
    prune_journal(sdir, now)
    return 0
