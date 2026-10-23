"""Durable SQLite spool (contract C1; ARCHITECTURE §2-§5, §7-§9, §17).

Every webhook is committed here before its sender gets a 2xx. Events are coalesced
per ``(route, key)`` into batches; a batch seals on size inside the enqueue
transaction and on readiness in ``seal_ready`` (the dispatcher's per-poll pass),
independent of any destination gate. Sealed content is frozen and hashed.

Durability: WAL + ``synchronous=FULL``, one ``BEGIN IMMEDIATE`` transaction per
write, so a process killed at any line leaves either the whole write or none of it.
``_test_hook`` points let tests SIGKILL a process at exact lines.

Fencing: ``claim`` writes ``lease_owner``; every ``mark_*`` is
``WHERE state='leased' AND lease_owner=?``. A worker that lost its lease gets
``LeaseLost``, nothing it reported is applied, and its attempt row is finished
``discarded``. Script emits are enqueued in the same transaction as the mark.

Nothing here ever discards a batch on its own: ``dead`` waits for an operator and
``dropped`` is written only by ``drop``/``split``.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import secrets as _secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator

from . import config as _config

STATES = ("open", "sealed", "leased", "retry", "delivered", "dead", "dropped")
PENDING = ("open", "sealed", "leased", "retry")
CLASSES = ("transport", "throttle", "batch", "config", "permanent", "poison", "quarantine")
QUARANTINE = "quarantine"
KEY_MAX_BYTES = 512
ERROR_MAX = 500
SCHEMA_VERSION = 2
MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
EMIT_KEYS = ("source", "key", "action", "payload", "route", "event_type", "dedup_id",
             "quiet_s", "max_s", "max_events", "max_bytes", "not_before")
DEST_FIELDS = ("state", "consecutive_failures", "opened_at", "cooldown_s", "probe_until",
               "last_probe_batch_id", "paused_at", "paused_by", "last_ok_at", "last_error",
               "retry_after_until")
HEARTBEAT_STALE_S = 30.0

# Test hook: called with a point name inside write transactions so tests can SIGKILL
# the process at an exact line. Always None in production.
# Points: enqueue:after_batch, enqueue:after_event, enqueue:after_seal, enqueue:after_tee, seal:mid,
#         claim:after_update, deliver:after_emits, mark:after_update, recover:mid,
#         replay:mid, quarantine:mid, reclassify:mid, split:mid, prune:mid
_test_hook: Callable[[str], None] | None = None


def _hook(point: str) -> None:
    if _test_hook is not None:
        _test_hook(point)


class SpoolError(Exception):
    """The write could not be made durable. Intake callers answer 503."""


class InvalidEvent(ValueError):
    """The event (or a script emit) breaks the enqueue rules. Never a 503: the intake
    quarantines it, the dispatcher treats a bad emit as a batch-class failure."""


class LeaseLost(Exception):
    """The worker no longer holds the batch's lease; nothing it reported was applied."""


class SchemaError(Exception):
    """The database schema is older or newer than this code (exit 2)."""


class LockHeld(Exception):
    """Another process holds dispatch.lock (exit 3)."""


@dataclass(frozen=True)
class EnqueueResult:
    event_id: int
    batch_id: int | None     # None only for a duplicate whose original event was pruned
    duplicate: bool = False  # dedup hit; nothing changed (intake answers "duplicate")
    replayed: bool = False   # dedup hit on a dead batch, which was replayed ("replayed")
    tees: tuple = ()         # one EnqueueResult per tee copy, in the order the tees were given


@dataclass(frozen=True)
class Attempt:
    id: int
    n: int
    delivery_id: str


# ---- pure helpers -------------------------------------------------------------------------
def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def content_hash(events: list, event_meta: list) -> str:
    """SHA-256 over the canonical JSON of the frozen part of the document."""
    return "sha256:" + hashlib.sha256(canonical({"events": events, "event_meta": event_meta})).hexdigest()


def is_ready(b: dict, now: float) -> bool:
    """Readiness of an ``open`` batch (ARCHITECTURE §5)."""
    if b["state"] != "open":
        return False
    return ((b["quiet_s"] == 0 and b["max_s"] == 0)
            or now >= b["last_at"] + b["quiet_s"]
            or now >= b["first_at"] + b["max_s"])


_CTRL = re.compile(r"[\x00-\x1f\x7f]+")


def scrub(text: Any, limit: int = ERROR_MAX) -> str:
    """Short, single-line error text for last_error / attempts.error."""
    return _CTRL.sub(" ", str(text or ""))[:limit]


def _num_or_none(name: str, v: Any, *, integer: bool = False, minimum: float = 0) -> float | int | None:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (integer and not isinstance(v, int)):
        raise InvalidEvent(f"{name} must be {'an integer' if integer else 'a number'}")
    if v < minimum or v != v:
        raise InvalidEvent(f"{name} must be >= {minimum}")
    return int(v) if integer else float(v)


def acquire_lock(path: str):
    """Exclusive non-blocking flock on ``path`` (dispatch.lock). Returns the open file;
    keep it open for as long as the lock is needed. Raises LockHeld."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
    f = open(path, "a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise LockHeld(f"{path} is held by another process") from None
    return f


def _split_sql(sql: str) -> list[str]:
    out, buf = [], ""
    for line in sql.splitlines(keepends=True):
        if not buf and line.strip().startswith("--"):
            continue
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    if buf.strip():
        raise SchemaError("migration ends with an incomplete statement")
    return out


def migrations() -> list[tuple[int, str]]:
    out = []
    for name in sorted(os.listdir(MIGRATIONS_DIR)):
        m = re.match(r"^(\d{4})_[A-Za-z0-9_]+\.sql$", name)
        if m:
            out.append((int(m.group(1)), os.path.join(MIGRATIONS_DIR, name)))
    return out


class Store:
    def __init__(self, path: str | None = None, config: dict | None = None,
                 now: Callable[[], float] = time.time, *, busy_timeout_s: float = 30,
                 span_for: Callable[[str], str | None] | None = None, durable: bool = True):
        self.config = config if config is not None else _config.load_config()
        self.path = path or _config.db_path(self.config)
        self.now = now
        self.busy_timeout_s = float(busy_timeout_s)
        self.span_for = span_for          # plugin default span per source (intake supplies it)
        self._durable = durable           # False only in tests that need speed over fsync
        self._local = threading.local()
        self._instance_id: str | None = None

    # ---- connection ------------------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is not None and getattr(self._local, "pid", None) == os.getpid():
            return c
        d = os.path.dirname(os.path.abspath(self.path))
        if not os.path.isdir(d):
            os.makedirs(d, mode=0o700, exist_ok=True)
        existed = os.path.exists(self.path)
        c = sqlite3.connect(self.path, timeout=self.busy_timeout_s, isolation_level=None,
                            check_same_thread=True)
        c.row_factory = sqlite3.Row
        c.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_s * 1000)}")
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=FULL" if self._durable else "PRAGMA synchronous=OFF")
        c.execute("PRAGMA foreign_keys=ON")
        if not existed:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        self._local.conn = c
        self._local.pid = os.getpid()
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    def _write(self, fn):
        """Run fn(conn) inside BEGIN IMMEDIATE ... COMMIT; roll back on any error.

        sqlite/OS failures become SpoolError; InvalidEvent, LeaseLost and the like pass through.
        """
        try:
            c = self._conn()
            c.execute("BEGIN IMMEDIATE")
        except (sqlite3.Error, OSError) as e:
            raise SpoolError(f"spool write failed: {e.__class__.__name__}: {e}") from e
        try:
            out = fn(c)
            c.execute("COMMIT")
        except BaseException as e:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if isinstance(e, (sqlite3.Error, OSError)):
                raise SpoolError(f"spool write failed: {e.__class__.__name__}: {e}") from e
            raise
        return out

    def _read(self, sql: str, args: Iterable = ()) -> list[sqlite3.Row]:
        return self._conn().execute(sql, tuple(args)).fetchall()

    # ---- schema (§17) ----------------------------------------------------------------------------
    def schema_version(self) -> int:
        c = self._conn()
        v = int(c.execute("PRAGMA user_version").fetchone()[0])
        if v == 0 and c.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone():
            row = c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            v = int(row[0]) if row else 0
        return v

    def check_schema(self) -> int:
        """For the intake and the CLI: refuse any version but this code's. Never creates
        the database file (a probe of a missing path must not leave an empty spool behind)."""
        if not os.path.exists(self.path):
            raise SchemaError(f"no spool at {self.path}: run `hookspool migrate` (stop the dispatcher first;"
                              " see deploy/README.md, Upgrading)")
        v = self.schema_version()
        if v < SCHEMA_VERSION:
            raise SchemaError(f"spool schema {v} is older than {SCHEMA_VERSION}: run `hookspool migrate`"
                              " (stop the dispatcher first; see deploy/README.md, Upgrading)")
        if v > SCHEMA_VERSION:
            raise SchemaError(f"spool schema {v} is newer than this hookspool ({SCHEMA_VERSION});"
                              " downgrade is not supported")
        return v

    def ensure_schema(self) -> list[int]:
        """Apply pending migrations, one BEGIN IMMEDIATE each. Callers hold dispatch.lock
        (only the dispatcher at startup and `hookspool migrate` call this)."""
        cur = self.schema_version()
        if cur > SCHEMA_VERSION:
            raise SchemaError(f"spool schema {cur} is newer than this hookspool ({SCHEMA_VERSION})")
        applied = []
        c = self._conn()
        for n, path in migrations():
            if n <= cur:
                continue
            with open(path, encoding="utf-8") as f:
                stmts = _split_sql(f.read())
            c.execute("BEGIN IMMEDIATE")
            try:
                for s in stmts:
                    c.execute(s)
                c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)", (str(n),))
                c.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('instance_id', ?)",
                          (_secrets.token_hex(6),))
                c.execute(f"PRAGMA user_version={int(n)}")
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
            applied.append(n)
        return applied

    def quick_check(self) -> bool:
        return self._conn().execute("PRAGMA quick_check").fetchone()[0] == "ok"

    def instance_id(self) -> str:
        if self._instance_id is None:
            row = self._conn().execute("SELECT value FROM meta WHERE key='instance_id'").fetchone()
            if row is None:
                raise SchemaError("spool has no instance_id: run `hookspool migrate`")
            self._instance_id = str(row[0])
        return self._instance_id

    # ---- meta ---------------------------------------------------------------------------------
    def meta_get(self, key: str) -> str | None:
        row = self._conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row[0]

    def meta_set(self, values: dict[str, Any]) -> None:
        def fn(c):
            for k, v in values.items():
                c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                          (k, None if v is None else str(v)))
        self._write(fn)

    # ---- validation ------------------------------------------------------------------------------
    def _prepare(self, source, key, action, payload, *, route=None, event_type="", dedup_id=None,
                 raw=None, headers=None, quiet_s=None, max_s=None, max_events=None, max_bytes=None,
                 not_before=None) -> dict:
        for name, v in (("source", source), ("key", key), ("action", action)):
            if not isinstance(v, str) or not v:
                raise InvalidEvent(f"{name} must be a non-empty string")
        if len(key.encode("utf-8")) > KEY_MAX_BYTES:
            raise InvalidEvent(f"key is longer than {KEY_MAX_BYTES} bytes")
        if action == QUARANTINE or any(ch.isspace() for ch in action):
            raise InvalidEvent("action is not a valid destination name")
        route = source if route is None else route
        if not isinstance(route, str) or not route:
            raise InvalidEvent("route must be a non-empty string")
        if not isinstance(payload, dict):
            raise InvalidEvent("payload must be a JSON object")
        if event_type is None:
            event_type = ""
        if not isinstance(event_type, str) or len(event_type) > 200:
            raise InvalidEvent("event_type must be a string of at most 200 characters")
        if dedup_id is not None:
            if isinstance(dedup_id, (dict, list, bool)):
                raise InvalidEvent("dedup_id must be a string")
            dedup_id = str(dedup_id) or None
            if dedup_id is not None and len(dedup_id) > 512:
                raise InvalidEvent("dedup_id is longer than 512 characters")
        if raw is not None:
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            if not isinstance(raw, (bytes, bytearray, memoryview)):
                raise InvalidEvent("raw must be bytes")
            raw = bytes(raw)
        if raw is not None and not self._store_raw(source):
            raw = None
        hdrs = None
        if headers is not None:
            if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                        for k, v in headers.items()):
                raise InvalidEvent("headers must be a mapping of strings")
            hdrs = json.dumps(headers, separators=(",", ":"), ensure_ascii=False)
        win = {"quiet_s": _num_or_none("quiet_s", quiet_s), "max_s": _num_or_none("max_s", max_s),
               "max_events": _num_or_none("max_events", max_events, integer=True, minimum=1),
               "max_bytes": _num_or_none("max_bytes", max_bytes, integer=True, minimum=1)}
        if any(v is None for v in win.values()):
            plugin_span = self.span_for(source) if self.span_for else None
            d = _config.window_for(self.config, source, route, plugin_span=plugin_span)
            win = {k: (d[k] if v is None else v) for k, v in win.items()}
        nb = _num_or_none("not_before", not_before)
        try:
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as e:
            raise InvalidEvent(f"payload is not JSON-serializable: {e.__class__.__name__}") from None
        return {"source": source, "key": key, "action": action, "route": route, "payload": body,
                "size": len(body.encode("utf-8")), "event_type": event_type, "dedup_id": dedup_id,
                "raw": raw, "headers": hdrs, "not_before": nb, **win}

    def _store_raw(self, source: str) -> bool:
        s = _config.source_cfg(self.config, source)
        if s.get("store_raw") is not None:
            return bool(s["store_raw"])
        return bool((self.config.get("spool") or {}).get("store_raw", True))

    def _emit(self, e: Any) -> dict:
        """Validate one script emit (the object inside ``{"enqueue": {...}}``)."""
        if not isinstance(e, dict):
            raise InvalidEvent("emit must be an object of enqueue fields")
        unknown = set(e) - set(EMIT_KEYS)
        if unknown:
            raise InvalidEvent(f"emit has unknown keys: {sorted(unknown)}")
        for k in ("source", "key", "payload"):
            if k not in e:
                raise InvalidEvent(f"emit is missing {k!r}")
        action = e.get("action")
        if action is None:
            src = e.get("source")
            action = (_config.route_destination(self.config, e["route"]) if e.get("route")
                      else _config.source_cfg(self.config, src if isinstance(src, str) else "").get("action"))
            if action is None:
                raise InvalidEvent("emit has no action and its route or source names no destination")
        kw = {k: e[k] for k in EMIT_KEYS if k in e and k not in ("source", "key", "action", "payload")}
        return self._prepare(e["source"], e["key"], action, e["payload"], **kw)

    def _tee_copy(self, ev: dict, route: Any, action: Any) -> dict:
        """A prepared event's copy for a tee route: the same request, its own route, destination
        and window (inherited from that route as usual)."""
        for name, v in (("tee route", route), ("tee destination", action)):
            if not isinstance(v, str) or not v:
                raise InvalidEvent(f"{name} must be a non-empty string")
        if action == QUARANTINE or any(ch.isspace() for ch in action):
            raise InvalidEvent("tee destination is not a valid destination name")
        plugin_span = self.span_for(ev["source"]) if self.span_for else None
        win = _config.window_for(self.config, ev["source"], route, plugin_span=plugin_span)
        return {**ev, "route": route, "action": action, **{k: win[k] for k in _config.WINDOW_FIELDS}}

    # ---- enqueue (§5, §7) -------------------------------------------------------------------------
    def enqueue(self, source: str, key: str, action: str, payload: dict, *, route: str | None = None,
                event_type: str = "", dedup_id: str | None = None, raw: bytes | None = None,
                headers: dict | None = None, quiet_s: float | None = None, max_s: float | None = None,
                max_events: int | None = None, max_bytes: int | None = None,
                not_before: float | None = None, tees: Iterable[tuple[str, str]] = ()) -> EnqueueResult:
        """Durably record one event; COMMIT has happened when this returns.

        Raises InvalidEvent for a malformed event, SpoolError when the write cannot be made
        durable. Window fields left None inherit route -> source -> span -> [coalesce].

        ``tees`` are ``(route, destination)`` pairs: each gets its own copy of the event, in its
        own batch with that route's window, in the same transaction (all copies or none). The
        explicit window arguments apply to the primary copy only. Dedup runs once, on the
        primary: a duplicate request makes no copies.
        """
        ev = self._prepare(source, key, action, payload, route=route, event_type=event_type,
                           dedup_id=dedup_id, raw=raw, headers=headers, quiet_s=quiet_s, max_s=max_s,
                           max_events=max_events, max_bytes=max_bytes, not_before=not_before)
        copies = [self._tee_copy(ev, r, d) for r, d in tees]
        return self._write(lambda c: self._enqueue_txn(c, self.now(), ev, copies))

    def _enqueue_txn(self, c: sqlite3.Connection, now: float, ev: dict, tees: Iterable[dict] = ()) -> EnqueueResult:
        src, dd = ev["source"], ev["dedup_id"]
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_enqueue_at', ?)", (repr(now),))
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                  (f"source:{src}:last_event_at", repr(now)))
        repoint = False
        if dd is not None:
            hit = c.execute("SELECT event_id FROM dedup WHERE source=? AND dedup_id=?", (src, dd)).fetchone()
            if hit is not None:
                eid = int(hit[0])
                row = c.execute("SELECT e.batch_id, b.state, b.destination FROM events e"
                                " JOIN batches b ON b.id = e.batch_id WHERE e.id=?", (eid,)).fetchone()
                state = row["state"] if row else None
                if state == "dropped":
                    repoint = True                       # the operator discarded it; enqueue anew
                else:
                    c.execute("UPDATE dedup SET dup_count = dup_count + 1 WHERE source=? AND dedup_id=?",
                              (src, dd))
                    bid = int(row["batch_id"]) if row else None
                    if state == "dead" and row["destination"] != QUARANTINE:
                        self._replay_txn(c, bid, now)
                        return EnqueueResult(eid, bid, duplicate=False, replayed=True)
                    return EnqueueResult(eid, bid, duplicate=True)

        eid, bid = self._place(c, now, ev, dedup="repoint" if repoint else "insert" if dd is not None else None)
        copies = []
        for t in tees:
            teid, tbid = self._place(c, now, t)
            copies.append(EnqueueResult(teid, tbid))
            _hook("enqueue:after_tee")
        return EnqueueResult(eid, bid, tees=tuple(copies))

    def _place(self, c: sqlite3.Connection, now: float, ev: dict, *, dedup: str | None = None) -> tuple[int, int]:
        """Append one prepared event to the open batch of its ``(route, key)``, or open one.
        ``dedup`` is ``insert`` or ``repoint`` for the copy the dedup row points at."""
        src, dd = ev["source"], ev["dedup_id"]
        b = c.execute("SELECT id, destination, count, bytes, max_bytes, not_before FROM batches"
                      " WHERE route=? AND key=? AND state='open' ORDER BY id DESC LIMIT 1",
                      (ev["route"], ev["key"])).fetchone()
        if b is not None and (b["destination"] != ev["action"]
                              or (b["count"] >= 1 and b["bytes"] + ev["size"] > b["max_bytes"])):
            # pre-join seal: keeps max_bytes a true bound (and one destination per batch)
            self._seal(c, int(b["id"]), now)
            _hook("enqueue:after_seal")
            b = None
        if b is not None:
            bid = int(b["id"])
            nb = b["not_before"]
            if ev["not_before"] is not None:
                nb = ev["not_before"] if nb is None else max(nb, ev["not_before"])
            c.execute("UPDATE batches SET last_at = MAX(last_at, ?), count = count + 1, bytes = bytes + ?,"
                      " not_before = ? WHERE id=?", (now, ev["size"], nb, bid))
        else:
            cur = c.execute(
                "INSERT INTO batches (route, source, destination, key, state, first_at, last_at, quiet_s,"
                " max_s, max_events, max_bytes, not_before, count, bytes)"
                " VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (ev["route"], src, ev["action"], ev["key"], now, now, ev["quiet_s"], ev["max_s"],
                 ev["max_events"], ev["max_bytes"], ev["not_before"], ev["size"]))
            bid = int(cur.lastrowid)
        _hook("enqueue:after_batch")
        cur = c.execute("INSERT INTO events (source, route, batch_id, event_type, dedup_id, received_at,"
                        " payload, raw, headers, size) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (src, ev["route"], bid, ev["event_type"], dd, now, ev["payload"], ev["raw"],
                         ev["headers"], ev["size"]))
        eid = int(cur.lastrowid)
        if dedup == "repoint":
            c.execute("UPDATE dedup SET event_id=?, seen_at=? WHERE source=? AND dedup_id=?", (eid, now, src, dd))
        elif dedup == "insert":
            c.execute("INSERT INTO dedup (source, dedup_id, event_id, seen_at) VALUES (?, ?, ?, ?)",
                      (src, dd, eid, now))
        _hook("enqueue:after_event")
        row = c.execute("SELECT count, bytes, max_events, max_bytes FROM batches WHERE id=?", (bid,)).fetchone()
        if row["count"] >= row["max_events"] or row["bytes"] >= row["max_bytes"]:
            self._seal(c, bid, now)                      # post-join seal
            _hook("enqueue:after_seal")
        return eid, bid

    # ---- sealing -------------------------------------------------------------------------------------
    @staticmethod
    def _frozen(c: sqlite3.Connection, batch_id: int) -> tuple[list, list]:
        rows = c.execute("SELECT id, event_type, received_at, dedup_id, payload FROM events"
                         " WHERE batch_id=? AND excluded_at IS NULL ORDER BY id", (batch_id,)).fetchall()
        events = [json.loads(r["payload"]) if r["payload"] is not None else None for r in rows]
        meta = [{"id": r["id"], "event_type": r["event_type"], "received_at": r["received_at"],
                 "dedup_id": r["dedup_id"]} for r in rows]
        return events, meta

    def _seal(self, c: sqlite3.Connection, batch_id: int, now: float) -> None:
        events, meta = self._frozen(c, batch_id)
        c.execute("UPDATE batches SET state='sealed', sealed_at=?, content_hash=? WHERE id=? AND state='open'",
                  (now, content_hash(events, meta), batch_id))

    def seal_ready(self, now: float | None = None, *, chunk: int = 50) -> list[int]:
        """Seal every ready ``open`` batch (the dispatcher calls this first on each poll,
        before any destination gate). Returns the sealed ids."""
        now = self.now() if now is None else now
        sealed: list[int] = []
        while True:
            def fn(c):
                ids = [int(r[0]) for r in c.execute(
                    "SELECT id FROM batches WHERE state='open' AND ((quiet_s = 0 AND max_s = 0)"
                    " OR ? >= last_at + quiet_s OR ? >= first_at + max_s) ORDER BY id LIMIT ?",
                    (now, now, chunk))]
                for i, bid in enumerate(ids):
                    self._seal(c, bid, now)
                    if i == 0:
                        _hook("seal:mid")
                return ids
            ids = self._write(fn)
            sealed += ids
            if len(ids) < chunk:
                return sealed

    # ---- quarantine (§9) ---------------------------------------------------------------------------------
    def quarantine(self, source: str, raw: bytes | None, headers: dict | None, error: str, *,
                   auth_headers: Iterable[str] = ()) -> tuple[int, int]:
        """Store a verified request our plugin code could not classify, as a ``dead``
        quarantine batch, and open ``config:plugin:<source>``. Returns (event_id, batch_id)."""
        if not isinstance(source, str) or not source:
            raise InvalidEvent("source must be a non-empty string")
        raw_b = b"" if raw is None else (raw.encode("utf-8") if isinstance(raw, str) else bytes(raw))
        strip = {h.lower() for h in auth_headers}
        hdrs = {str(k): str(v) for k, v in (headers or {}).items() if str(k).lower() not in strip}
        hjson = json.dumps(hdrs, separators=(",", ":"), ensure_ascii=False)
        err = scrub(error)
        win = _config.merge(_config.DEFAULTS["coalesce"], self.config.get("coalesce") or {})

        def fn(c):
            now = self.now()
            c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                      (f"source:{source}:last_event_at", repr(now)))
            cur = c.execute(
                "INSERT INTO batches (route, source, destination, key, state, first_at, last_at, quiet_s, max_s,"
                " max_events, max_bytes, count, bytes, sealed_at, last_error, last_class)"
                " VALUES (?, ?, ?, ?, 'dead', ?, ?, 0, 0, ?, ?, 1, ?, ?, ?, 'quarantine')",
                (source, source, QUARANTINE, QUARANTINE, now, now, int(win["max_events"]), int(win["max_bytes"]),
                 len(raw_b), now, err))
            bid = int(cur.lastrowid)
            _hook("quarantine:mid")
            cur = c.execute("INSERT INTO events (source, route, batch_id, event_type, received_at, payload, raw,"
                            " headers, size) VALUES (?, ?, ?, '', ?, NULL, ?, ?, ?)",
                            (source, source, bid, now, raw_b, hjson, len(raw_b)))
            self._issue_open_txn(c, f"config:plugin:{source}", "config", now, route=source,
                                 sample_batch_id=bid, error=err)
            return int(cur.lastrowid), bid
        return self._write(fn)

    # ---- dispatcher side (§6) -----------------------------------------------------------------------------
    def candidates(self, now: float | None = None, limit: int | None = None) -> list[dict]:
        """Head-of-group batches that may be claimed now, oldest first (no destination gate).

        Per (route, key) only the oldest unfinished batch (open, sealed, retry, leased) is
        considered, and only if it is sealed, or retry and due, and not_before has passed.
        """
        now = self.now() if now is None else now
        q = ("SELECT b.* FROM batches b WHERE b.state IN ('sealed', 'retry')"
             " AND b.id = (SELECT b2.id FROM batches b2 WHERE b2.route = b.route AND b2.key = b.key"
             "  AND b2.state IN ('open', 'sealed', 'retry', 'leased') ORDER BY b2.first_at, b2.id LIMIT 1)"
             " AND (b.state = 'sealed' OR b.next_attempt_at IS NULL OR b.next_attempt_at <= ?)"
             " AND (b.not_before IS NULL OR b.not_before <= ?)"
             " ORDER BY b.first_at, b.id")
        args: list = [now, now]
        if limit is not None:
            q += " LIMIT ?"
            args.append(int(limit))
        return [dict(r) for r in self._read(q, args)]

    def claim(self, batch_id: int, lease_s: float, owner: str, *, now: float | None = None,
              probe: bool = False) -> bool:
        """sealed/retry -> leased, fenced by ``owner`` (unique per claim). A probe claim also
        writes the destination's ``last_probe_batch_id`` and ``probe_until`` in the same txn."""
        if not isinstance(owner, str) or not owner:
            raise ValueError("owner must be a non-empty string")
        now = self.now() if now is None else now
        until = now + float(lease_s)

        def fn(c):
            cur = c.execute("UPDATE batches SET state='leased', lease_until=?, lease_owner=? WHERE id=?"
                            " AND state IN ('sealed', 'retry')", (until, owner, batch_id))
            if cur.rowcount != 1:
                return False
            _hook("claim:after_update")
            if probe:
                dest = c.execute("SELECT destination FROM batches WHERE id=?", (batch_id,)).fetchone()[0]
                self._dest_upsert(c, dest, {"last_probe_batch_id": batch_id, "probe_until": until})
            return True
        return self._write(fn)

    def start_attempt(self, batch_id: int, owner: str, *, per_attempt: bool = False, probe: bool = False,
                      now: float | None = None) -> Attempt:
        """Write the attempt row before the request is made. ``per_attempt`` picks the
        delivery id form (Hermes: per attempt; script/http: per batch)."""
        now = self.now() if now is None else now
        inst = self.instance_id()

        def fn(c):
            b = c.execute("SELECT destination, state, lease_owner FROM batches WHERE id=?", (batch_id,)).fetchone()
            if b is None or b["state"] != "leased" or b["lease_owner"] != owner:
                return None
            n = int(c.execute("SELECT COALESCE(MAX(n), 0) + 1 FROM attempts WHERE batch_id=?",
                              (batch_id,)).fetchone()[0])
            cur = c.execute("INSERT INTO attempts (batch_id, n, destination, started_at, probe)"
                            " VALUES (?, ?, ?, ?, ?)", (batch_id, n, b["destination"], now, int(bool(probe))))
            aid = int(cur.lastrowid)
            did = f"hs_{inst}_{batch_id}_{aid}" if per_attempt else f"hs_{inst}_{batch_id}"
            c.execute("UPDATE attempts SET delivery_id=? WHERE id=?", (did, aid))
            return Attempt(aid, n, did)
        out = self._write(fn)
        if out is None:
            raise LeaseLost(f"batch {batch_id} is not leased by this owner")
        return out

    @staticmethod
    def _finish_attempt_txn(c, attempt_id, now, outcome, klass, status, error, duration_ms, counted,
                            events_delivered=None, failed_event_id=None):
        c.execute("UPDATE attempts SET finished_at=?, outcome=?, klass=?, status=?, error=?, duration_ms=?,"
                  " counted=?, events_delivered=?, failed_event_id=? WHERE id=? AND finished_at IS NULL",
                  (now, outcome, klass, status, None if error is None else scrub(error),
                   None if duration_ms is None else int(duration_ms), int(bool(counted)),
                   None if events_delivered is None else int(events_delivered),
                   None if failed_event_id is None else int(failed_event_id), attempt_id))

    def finish_attempt(self, attempt_id: int, outcome: str, *, klass: str | None = None,
                       status: int | None = None, error: str | None = None, duration_ms: float | None = None,
                       counted: bool = False, now: float | None = None) -> None:
        if outcome not in ("ok", "retry", "dead", "discarded"):
            raise ValueError(f"bad outcome {outcome!r}")
        now = self.now() if now is None else now
        self._write(lambda c: self._finish_attempt_txn(c, attempt_id, now, outcome, klass, status, error,
                                                       duration_ms, counted))

    def _fenced(self, batch_id, owner, now, attempt_id, attempt_fields, update_sql, update_args, after=None):
        """Run the fenced batch update; on success finish the attempt and run ``after``
        in the same transaction. On a lost lease finish the attempt ``discarded``,
        commit that alone, and raise LeaseLost."""
        def fn(c):
            cur = c.execute(update_sql + " WHERE id=? AND state='leased' AND lease_owner=?",
                            (*update_args, batch_id, owner))
            if cur.rowcount != 1:
                if attempt_id is not None:
                    o = dict(attempt_fields, outcome="discarded", counted=False)
                    self._finish_attempt_txn(c, attempt_id, now, **o)
                return False
            _hook("mark:after_update")
            if attempt_id is not None:
                self._finish_attempt_txn(c, attempt_id, now, **attempt_fields)
            if after is not None:
                after(c)
            return True
        if not self._write(fn):
            raise LeaseLost(f"batch {batch_id}: lease lost; result not applied")

    def mark_delivered(self, batch_id: int, owner: str, result: str = "", emits: list | None = None, *,
                       attempt_id: int | None = None, status: int | None = None,
                       duration_ms: float | None = None, now: float | None = None,
                       events_delivered: int | None = None,
                       failed_event_id: int | None = None) -> list[EnqueueResult]:
        """leased -> delivered; the emits are enqueued in the same fenced transaction.
        Raises InvalidEvent (before writing anything) if any emit is malformed."""
        now = self.now() if now is None else now
        prepared = [self._emit(e) for e in (emits or [])]
        results: list[EnqueueResult] = []

        def after(c):
            for ev in prepared:
                results.append(self._enqueue_txn(c, now, ev))
            _hook("deliver:after_emits")
        self._fenced(batch_id, owner, now, attempt_id,
                     {"outcome": "ok", "klass": None, "status": status, "error": None,
                      "duration_ms": duration_ms, "counted": True, "events_delivered": events_delivered,
                      "failed_event_id": failed_event_id},
                     "UPDATE batches SET state='delivered', delivered_at=?, result=?, attempts = attempts + 1,"
                     " last_error=NULL, last_class=NULL, next_attempt_at=NULL, lease_until=NULL, lease_owner=NULL",
                     (now, scrub(result)), after)
        return results

    def mark_retry(self, batch_id: int, owner: str, error: str, klass: str, next_at: float, counted: bool, *,
                   attempt_id: int | None = None, status: int | None = None,
                   duration_ms: float | None = None, now: float | None = None,
                   events_delivered: int | None = None, failed_event_id: int | None = None) -> None:
        if klass not in ("transport", "throttle", "batch", "config"):
            raise ValueError(f"retry class must be transport, throttle, batch or config, not {klass!r}")
        now = self.now() if now is None else now
        self._fenced(batch_id, owner, now, attempt_id,
                     {"outcome": "retry", "klass": klass, "status": status, "error": error,
                      "duration_ms": duration_ms, "counted": counted, "events_delivered": events_delivered,
                      "failed_event_id": failed_event_id},
                     "UPDATE batches SET state='retry', next_attempt_at=?, attempts = attempts + ?, last_error=?,"
                     " last_class=?, lease_until=NULL, lease_owner=NULL",
                     (float(next_at), int(bool(counted)), scrub(error), klass))

    def mark_dead(self, batch_id: int, owner: str, error: str, klass: str, *, counted: bool = False,
                  attempt_id: int | None = None, status: int | None = None,
                  duration_ms: float | None = None, now: float | None = None,
                  events_delivered: int | None = None, failed_event_id: int | None = None) -> None:
        if klass not in ("permanent", "poison", "batch"):
            raise ValueError(f"dead class must be permanent, poison or batch, not {klass!r}")
        now = self.now() if now is None else now
        self._fenced(batch_id, owner, now, attempt_id,
                     {"outcome": "dead", "klass": klass, "status": status, "error": error,
                      "duration_ms": duration_ms, "counted": counted, "events_delivered": events_delivered,
                      "failed_event_id": failed_event_id},
                     "UPDATE batches SET state='dead', next_attempt_at=NULL, attempts = attempts + ?, last_error=?,"
                     " last_class=?, lease_until=NULL, lease_owner=NULL",
                     (int(bool(counted)), scrub(error), klass))

    def mark_dead_unleased(self, batch_id: int, error: str, klass: str = "poison",
                           now: float | None = None) -> bool:
        """sealed/retry -> dead without a lease (the poison-by-comparison rule marks the
        previous probe batch, which no worker holds)."""
        if klass not in ("permanent", "poison"):
            raise ValueError("klass must be permanent or poison")

        def fn(c):
            cur = c.execute("UPDATE batches SET state='dead', next_attempt_at=NULL, last_error=?, last_class=?"
                            " WHERE id=? AND state IN ('sealed', 'retry')", (scrub(error), klass, batch_id))
            return cur.rowcount == 1
        return self._write(fn)

    def recover_leases(self, now: float | None = None, live_batch_ids: Iterable[int] = (), *,
                       all_leases: bool = False) -> list[int]:
        """Leased batches no live worker holds -> retry, due now, uncounted.

        ``all_leases`` (dispatcher startup, where the flock proves every lease is a dead
        process's) recovers every leased row; otherwise only expired leases not in
        ``live_batch_ids`` are recovered."""
        now = self.now() if now is None else now
        live = set(int(i) for i in live_batch_ids)

        def fn(c):
            if all_leases:
                rows = c.execute("SELECT id FROM batches WHERE state='leased'").fetchall()
            else:
                rows = c.execute("SELECT id FROM batches WHERE state='leased' AND lease_until < ?",
                                 (now,)).fetchall()
            ids = [int(r[0]) for r in rows if int(r[0]) not in live]
            for bid in ids:
                c.execute("UPDATE batches SET state='retry', next_attempt_at=?, last_error='lease expired',"
                          " last_class='transport', lease_until=NULL, lease_owner=NULL"
                          " WHERE id=? AND state='leased'", (now, bid))
                _hook("recover:mid")
                c.execute("UPDATE attempts SET finished_at=?, outcome='retry', klass='transport',"
                          " error='lease expired', counted=0 WHERE batch_id=? AND finished_at IS NULL", (now, bid))
            return ids
        return self._write(fn)

    def redue_config_retries(self, now: float | None = None) -> int:
        """After a successful config reload: every config-class retry is due now."""
        now = self.now() if now is None else now
        return self._write(lambda c: c.execute(
            "UPDATE batches SET next_attempt_at=? WHERE state='retry' AND last_class='config'",
            (now,)).rowcount)

    # ---- operator actions (§4.1, §16) --------------------------------------------------------------------
    def _replay_txn(self, c, batch_id: int, now: float) -> bool:
        b = c.execute("SELECT destination, last_class FROM batches WHERE id=? AND state='dead'",
                      (batch_id,)).fetchone()
        if b is None or b["destination"] == QUARANTINE:
            return False
        c.execute("UPDATE batches SET state='retry', next_attempt_at=?, not_before=?, attempts=0, last_class=NULL"
                  " WHERE id=? AND state='dead'", (now, now, batch_id))
        _hook("replay:mid")
        self._resolve_dead_issues(c, b["destination"], now)
        return True

    def replay(self, batch_id: int, *, now: float | None = None,
               reclassify: Callable[[str, bytes, dict], dict | None] | None = None) -> bool:
        """dead -> retry, due now, attempts 0 (content stays frozen). On a quarantine batch,
        re-run classification through ``reclassify`` (see ``reclassify_quarantine``)."""
        b = self.batch(batch_id)
        if b is None or b["state"] != "dead":
            return False
        if b["destination"] == QUARANTINE:
            if reclassify is None:
                raise ValueError("a quarantine batch is replayed by reclassifying it; pass reclassify=")
            self.reclassify_quarantine(batch_id, reclassify, now=now)
            return True
        now = self.now() if now is None else now
        return self._write(lambda c: self._replay_txn(c, batch_id, now))

    def replay_dead(self, *, destination: str | None = None, source: str | None = None,
                    since: float | None = None, now: float | None = None) -> list[int]:
        """Bulk replay of dead (non-quarantine) batches; ``since`` filters on first_at."""
        now = self.now() if now is None else now
        q, args = "SELECT id FROM batches WHERE state='dead' AND destination != ?", [QUARANTINE]
        for col, v in (("destination", destination), ("source", source)):
            if v is not None:
                q += f" AND {col}=?"
                args.append(v)
        if since is not None:
            q += " AND first_at >= ?"
            args.append(float(since))

        def fn(c):
            ids = [int(r[0]) for r in c.execute(q + " ORDER BY first_at, id", args)]
            return [i for i in ids if self._replay_txn(c, i, now)]
        return self._write(fn)

    def reclassify_quarantine(self, batch_id: int, fn: Callable[[str, bytes, dict], dict | None], *,
                              now: float | None = None) -> list[EnqueueResult]:
        """Re-run classification of a quarantine batch's stored requests.

        ``fn(source, raw, headers)`` returns enqueue fields (``key``, ``action``, ``payload``,
        optional ``route``, ``event_type``, ``dedup_id``, window fields, and ``tees``, the
        ``(route, destination)`` pairs of the request's tee copies) or None for a request
        the plugin now ignores; it runs outside any transaction. If it raises, nothing
        changes. Otherwise every result is enqueued and the quarantine batch becomes
        ``delivered`` with ``result = 'reclassified'``, in one transaction."""
        b = self.batch(batch_id)
        if b is None or b["state"] != "dead" or b["destination"] != QUARANTINE:
            raise ValueError(f"batch {batch_id} is not a dead quarantine batch")
        rows = self._read("SELECT source, raw, headers FROM events WHERE batch_id=? ORDER BY id", (batch_id,))
        prepared = []
        for r in rows:
            out = fn(r["source"], bytes(r["raw"] or b""), json.loads(r["headers"] or "{}"))
            if out is None:
                continue
            if not isinstance(out, dict):
                raise InvalidEvent("reclassify must return a mapping of enqueue fields or None")
            e = dict(out)
            e.setdefault("source", r["source"])
            if "raw" in e:
                raw = e.pop("raw")
            else:
                raw = bytes(r["raw"] or b"")
            hdrs = e.pop("headers", None)
            tees = e.pop("tees", None) or ()
            ev = self._emit(e)
            if raw is not None and self._store_raw(ev["source"]):
                ev["raw"] = raw
            if hdrs is not None:
                ev["headers"] = json.dumps(hdrs, separators=(",", ":"), ensure_ascii=False)
            if not isinstance(tees, (list, tuple)) or not all(isinstance(t, (list, tuple)) and len(t) == 2
                                                              for t in tees):
                raise InvalidEvent("reclassify tees must be (route, destination) pairs")
            prepared.append((ev, [self._tee_copy(ev, r, d) for r, d in tees]))
        results: list[EnqueueResult] = []

        def txn(c):
            t = self.now() if now is None else now
            cur = c.execute("UPDATE batches SET state='delivered', delivered_at=?, result='reclassified'"
                            " WHERE id=? AND state='dead' AND destination=?", (t, batch_id, QUARANTINE))
            if cur.rowcount != 1:
                raise ValueError(f"batch {batch_id} changed while reclassifying")
            _hook("reclassify:mid")
            for ev, copies in prepared:
                results.append(self._enqueue_txn(c, t, ev, copies))
            self._resolve_quarantine_issue(c, b["source"], t)
        self._write(txn)
        return results

    def retry_now(self, batch_id: int, now: float | None = None) -> bool:
        now = self.now() if now is None else now
        return self._write(lambda c: c.execute(
            "UPDATE batches SET next_attempt_at=? WHERE id=? AND state='retry'", (now, batch_id)).rowcount == 1)

    def split(self, batch_id: int, *, max_events: int | None = None, max_bytes: int | None = None,
              at: int | None = None, now: float | None = None) -> list[int]:
        """Cut a sealed/retry/dead batch into new sealed batches of the same group
        (``not_before = now``); the original becomes ``dropped`` with ``result = 'split'``.
        Exactly one of ``max_events``, ``max_bytes``, ``at`` (first event id of the second
        part). Raises ValueError when there is nothing to split."""
        if sum(x is not None for x in (max_events, max_bytes, at)) != 1:
            raise ValueError("give exactly one of max_events, max_bytes, at")
        if max_events is not None and (not isinstance(max_events, int) or max_events < 1):
            raise ValueError("max_events must be >= 1")
        if max_bytes is not None and (not isinstance(max_bytes, int) or max_bytes < 1):
            raise ValueError("max_bytes must be >= 1")
        now = self.now() if now is None else now

        def fn(c):
            b = c.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            if b is None or b["state"] not in ("sealed", "retry", "dead") or b["destination"] == QUARANTINE:
                raise ValueError(f"batch {batch_id} cannot be split (needs sealed, retry or dead)")
            evs = c.execute("SELECT id, size, received_at, excluded_at FROM events WHERE batch_id=? ORDER BY id",
                            (batch_id,)).fetchall()
            parts: list[list] = []
            if at is not None:
                ids = [e["id"] for e in evs]
                if at not in ids or ids.index(at) == 0:
                    raise ValueError("--at must name an event of the batch other than its first")
                k = ids.index(at)
                parts = [list(evs[:k]), list(evs[k:])]
            else:
                cur_part: list = []
                n = size = 0
                for e in evs:
                    live = e["excluded_at"] is None
                    if cur_part and ((max_events is not None and live and n >= max_events)
                                     or (max_bytes is not None and live and size + e["size"] > max_bytes)):
                        parts.append(cur_part)
                        cur_part, n, size = [], 0, 0
                    cur_part.append(e)
                    if live:
                        n += 1
                        size += e["size"]
                if cur_part:
                    parts.append(cur_part)
            if len(parts) < 2:
                raise ValueError("nothing to split: the batch already fits")
            new_ids = []
            for part in parts:
                live = [e for e in part if e["excluded_at"] is None]
                cur = c.execute(
                    "INSERT INTO batches (route, source, destination, key, state, first_at, last_at, quiet_s,"
                    " max_s, max_events, max_bytes, not_before, count, bytes)"
                    " VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (b["route"], b["source"], b["destination"], b["key"], min(e["received_at"] for e in part),
                     max(e["received_at"] for e in part), b["quiet_s"], b["max_s"], b["max_events"],
                     b["max_bytes"], now, len(live), sum(e["size"] for e in live)))
                nid = int(cur.lastrowid)
                c.executemany("UPDATE events SET batch_id=? WHERE id=?", [(nid, e["id"]) for e in part])
                self._seal(c, nid, now)
                new_ids.append(nid)
                _hook("split:mid")
            c.execute("UPDATE batches SET state='dropped', dropped_at=?, count=0, bytes=0, next_attempt_at=NULL,"
                      " result=? WHERE id=?", (now, scrub("split into " + ",".join(map(str, new_ids))), batch_id))
            if b["state"] == "dead":
                self._resolve_dead_issues(c, b["destination"], now)
            return new_ids
        return self._write(fn)

    def exclude(self, batch_id: int, event_ids: Iterable[int], now: float | None = None) -> int:
        """Set excluded_at on events of an open/sealed/retry/dead batch and recompute
        count, bytes and (if sealed) content_hash. Returns how many were newly excluded."""
        ids = sorted({int(i) for i in event_ids})
        if not ids:
            raise ValueError("no event ids")
        now = self.now() if now is None else now

        def fn(c):
            b = c.execute("SELECT state, destination, sealed_at FROM batches WHERE id=?", (batch_id,)).fetchone()
            if b is None or b["state"] not in ("open", "sealed", "retry", "dead") or b["destination"] == QUARANTINE:
                raise ValueError(f"batch {batch_id} cannot be edited (needs open, sealed, retry or dead)")
            mine = {int(r[0]) for r in c.execute(
                f"SELECT id FROM events WHERE batch_id=? AND id IN ({','.join('?' * len(ids))})", (batch_id, *ids))}
            missing = set(ids) - mine
            if missing:
                raise ValueError(f"events {sorted(missing)} are not in batch {batch_id}")
            n = c.execute(f"UPDATE events SET excluded_at=? WHERE batch_id=? AND excluded_at IS NULL"
                          f" AND id IN ({','.join('?' * len(ids))})", (now, batch_id, *ids)).rowcount
            cnt, size = c.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM events WHERE batch_id=?"
                                  " AND excluded_at IS NULL", (batch_id,)).fetchone()
            h = None
            if b["sealed_at"] is not None:
                h = content_hash(*self._frozen(c, batch_id))
            c.execute("UPDATE batches SET count=?, bytes=?, content_hash=COALESCE(?, content_hash) WHERE id=?",
                      (cnt, size, h, batch_id))
            return n
        return self._write(fn)

    def drop(self, batch_id: int, now: float | None = None, reason: str = "") -> bool:
        """open/sealed/retry/dead -> dropped (operator only; never automatic)."""
        now = self.now() if now is None else now

        def fn(c):
            b = c.execute("SELECT state, destination, source FROM batches WHERE id=?", (batch_id,)).fetchone()
            if b is None or b["state"] not in ("open", "sealed", "retry", "dead"):
                return False
            c.execute("UPDATE batches SET state='dropped', dropped_at=?, next_attempt_at=NULL, result=? WHERE id=?",
                      (now, scrub("dropped" + (f": {reason}" if reason else "")), batch_id))
            if b["destination"] == QUARANTINE:
                self._resolve_quarantine_issue(c, b["source"], now)
            elif b["state"] == "dead":
                self._resolve_dead_issues(c, b["destination"], now)
            return True
        return self._write(fn)

    # ---- destinations ------------------------------------------------------------------------------------
    @staticmethod
    def _dest_upsert(c, name: str, fields: dict) -> None:
        bad = set(fields) - set(DEST_FIELDS)
        if bad:
            raise ValueError(f"unknown destination fields {sorted(bad)}")
        c.execute("INSERT OR IGNORE INTO destinations (name) VALUES (?)", (name,))
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE destinations SET {cols} WHERE name=?", (*fields.values(), name))

    def destination_get(self, name: str) -> dict:
        row = self._conn().execute("SELECT * FROM destinations WHERE name=?", (name,)).fetchone()
        if row is not None:
            return dict(row)
        d = {k: None for k in DEST_FIELDS}
        d.update(name=name, state="closed", consecutive_failures=0)
        return d

    def destination_set(self, name: str, **fields) -> dict:
        if "state" in fields and fields["state"] not in ("closed", "open", "half_open", "paused"):
            raise ValueError(f"bad destination state {fields['state']!r}")
        self._write(lambda c: self._dest_upsert(c, name, fields))
        return self.destination_get(name)

    def destination_list(self) -> list[dict]:
        return [dict(r) for r in self._read("SELECT * FROM destinations ORDER BY name")]

    # ---- issues (§11.4) ------------------------------------------------------------------------------------
    @staticmethod
    def _issue_open_txn(c, fingerprint, kind, now, *, destination=None, route=None, sample_batch_id=None,
                        error=None) -> tuple[int, bool]:
        row = c.execute("SELECT id, state FROM issues WHERE fingerprint=?", (fingerprint,)).fetchone()
        err = None if error is None else scrub(error)
        if row is None:
            cur = c.execute("INSERT INTO issues (fingerprint, kind, destination, route, state, first_at, last_at,"
                            " count, sample_batch_id, last_error) VALUES (?, ?, ?, ?, 'open', ?, ?, 1, ?, ?)",
                            (fingerprint, kind, destination, route, now, now, sample_batch_id, err))
            return int(cur.lastrowid), True
        reopen = row["state"] == "resolved"
        c.execute("UPDATE issues SET count = count + 1, last_at=?, last_error=COALESCE(?, last_error),"
                  " sample_batch_id=COALESCE(?, sample_batch_id),"
                  " state = CASE WHEN state='resolved' THEN 'open' ELSE state END,"
                  " alerted_at = CASE WHEN state='resolved' THEN NULL ELSE alerted_at END WHERE id=?",
                  (now, err, sample_batch_id, row["id"]))
        return int(row["id"]), reopen

    def issue_open(self, fingerprint: str, kind: str, *, destination: str | None = None, route: str | None = None,
                   sample_batch_id: int | None = None, error: str | None = None,
                   now: float | None = None) -> tuple[int, bool]:
        """Open (or bump) an issue. Returns (id, opened_or_reopened)."""
        now = self.now() if now is None else now
        return self._write(lambda c: self._issue_open_txn(c, fingerprint, kind, now, destination=destination,
                                                          route=route, sample_batch_id=sample_batch_id,
                                                          error=error))

    def _issue_set(self, ident: int | str, state: str, allowed_from: tuple, now: float | None) -> bool:
        now = self.now() if now is None else now
        col = "id" if isinstance(ident, int) else "fingerprint"
        marks = ",".join("?" * len(allowed_from))
        return self._write(lambda c: c.execute(
            f"UPDATE issues SET state=?, last_at=? WHERE {col}=? AND state IN ({marks})",
            (state, now, ident, *allowed_from)).rowcount == 1)

    def issue_resolve(self, ident: int | str, now: float | None = None) -> bool:
        return self._issue_set(ident, "resolved", ("open", "acked"), now)

    def issue_ack(self, ident: int | str, now: float | None = None) -> bool:
        return self._issue_set(ident, "acked", ("open",), now)

    def issue_ignore(self, ident: int | str, now: float | None = None) -> bool:
        return self._issue_set(ident, "ignored", ("open", "acked", "resolved"), now)

    def issue_get(self, ident: int | str) -> dict | None:
        col = "id" if isinstance(ident, int) else "fingerprint"
        row = self._conn().execute(f"SELECT * FROM issues WHERE {col}=?", (ident,)).fetchone()
        return dict(row) if row else None

    def issue_list(self, state: str | None = None) -> list[dict]:
        if state:
            return [dict(r) for r in self._read("SELECT * FROM issues WHERE state=? ORDER BY id", (state,))]
        return [dict(r) for r in self._read("SELECT * FROM issues ORDER BY id")]

    def issues_to_alert(self) -> list[dict]:
        return [dict(r) for r in self._read("SELECT * FROM issues WHERE state='open' AND alerted_at IS NULL"
                                            " ORDER BY id")]

    def issue_mark_alerted(self, issue_id: int, now: float | None = None) -> None:
        now = self.now() if now is None else now
        self._write(lambda c: c.execute("UPDATE issues SET alerted_at=? WHERE id=?", (now, issue_id)))

    @staticmethod
    def _resolve_dead_issues(c, destination: str, now: float) -> None:
        for klass in ("permanent", "poison"):
            left = c.execute("SELECT 1 FROM batches WHERE state='dead' AND destination=? AND last_class=? LIMIT 1",
                             (destination, klass)).fetchone()
            if left is None:
                c.execute("UPDATE issues SET state='resolved', last_at=? WHERE fingerprint=?"
                          " AND state IN ('open', 'acked')", (now, f"delivery:{destination}:{klass}"))

    @staticmethod
    def _resolve_quarantine_issue(c, source: str, now: float) -> None:
        left = c.execute("SELECT 1 FROM batches WHERE state='dead' AND destination=? AND source=? LIMIT 1",
                         (QUARANTINE, source)).fetchone()
        if left is None:
            c.execute("UPDATE issues SET state='resolved', last_at=? WHERE fingerprint=? AND state IN"
                      " ('open', 'acked')", (now, f"config:plugin:{source}"))

    # ---- alerts rate limit -----------------------------------------------------------------------------
    def alert_gate(self, kind: str, interval_s: float, now: float | None = None) -> tuple[bool, int]:
        """(send?, suppressed_since_last_send). Persisted, so restarts keep the rate limit."""
        now = self.now() if now is None else now

        def fn(c):
            row = c.execute("SELECT last_at, suppressed FROM alerts WHERE kind=?", (kind,)).fetchone()
            if row is None:
                c.execute("INSERT INTO alerts (kind, last_at, suppressed) VALUES (?, ?, 0)", (kind, now))
                return True, 0
            if now - row["last_at"] >= interval_s:
                c.execute("UPDATE alerts SET last_at=?, suppressed=0 WHERE kind=?", (now, kind))
                return True, int(row["suppressed"])
            c.execute("UPDATE alerts SET suppressed = suppressed + 1 WHERE kind=?", (kind,))
            return False, int(row["suppressed"]) + 1
        return self._write(fn)

    def alert_open_at(self, kind: str, interval_s: float) -> float:
        """When ``alert_gate(kind)`` next lets an alert through (0 if it would now)."""
        row = self._conn().execute("SELECT last_at FROM alerts WHERE kind=?", (kind,)).fetchone()
        return 0.0 if row is None else float(row["last_at"]) + float(interval_s)

    # ---- retention (§8) ------------------------------------------------------------------------------------
    def prune(self, now: float | None = None, *, chunk: int = 1000) -> dict[str, int]:
        """Delete what retention allows, in small transactions; dead batches are never pruned."""
        now = self.now() if now is None else now
        ret = _config.merge(_config.DEFAULTS["retention"], self.config.get("retention") or {})
        day = _config.DAY
        out = {"delivered": 0, "dropped": 0, "events": 0, "attempts": 0, "dedup": 0, "issues": 0}

        def batches(state: str, col: str, cutoff: float, label: str) -> None:
            while True:
                def fn(c):
                    ids = [int(r[0]) for r in c.execute(
                        f"SELECT id FROM batches WHERE state=? AND {col} < ? ORDER BY id LIMIT ?",
                        (state, cutoff, chunk))]
                    if not ids:
                        return 0, 0
                    marks = ",".join("?" * len(ids))
                    ne = c.execute(f"DELETE FROM events WHERE batch_id IN ({marks})", ids).rowcount
                    _hook("prune:mid")
                    c.execute(f"DELETE FROM batches WHERE id IN ({marks})", ids)
                    return len(ids), ne
                nb, ne = self._write(fn)
                out[label] += nb
                out["events"] += ne
                if nb < chunk:
                    return

        def rows(sql_select: str, args: tuple, table: str, label: str) -> None:
            while True:
                n = self._write(lambda c: c.execute(
                    f"DELETE FROM {table} WHERE rowid IN ({sql_select} LIMIT {int(chunk)})", args).rowcount)
                out[label] += n
                if n < chunk:
                    return

        batches("delivered", "delivered_at", now - float(ret["delivered_days"]) * day, "delivered")
        batches("dropped", "dropped_at", now - float(ret["dropped_days"]) * day, "dropped")
        rows("SELECT rowid FROM attempts WHERE started_at < ? AND finished_at IS NOT NULL",
             (now - float(ret["attempts_days"]) * day,), "attempts", "attempts")
        for (src,) in self._read("SELECT DISTINCT source FROM dedup"):
            rows("SELECT rowid FROM dedup WHERE source=? AND seen_at < ?",
                 (src, now - _config.dedup_window_s(self.config, src)), "dedup", "dedup")
        rows("SELECT rowid FROM issues WHERE state IN ('resolved', 'ignored') AND last_at < ?",
             (now - float(ret["issues_days"]) * day,), "issues", "issues")
        try:
            self._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        except sqlite3.Error:
            pass
        return out

    def vacuum(self) -> None:
        self._conn().execute("VACUUM")

    # ---- reads -----------------------------------------------------------------------------------------
    def batch(self, batch_id: int) -> dict | None:
        row = self._conn().execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        return dict(row) if row else None

    def events(self, batch_id: int, *, include_excluded: bool = False, payload: bool = True) -> list[dict]:
        q = "SELECT id, source, route, event_type, dedup_id, received_at, size, excluded_at"
        q += ", payload" if payload else ""
        q += " FROM events WHERE batch_id=?"
        if not include_excluded:
            q += " AND excluded_at IS NULL"
        out = []
        for r in self._read(q + " ORDER BY id", (batch_id,)):
            d = dict(r)
            if payload:
                d["payload"] = None if d["payload"] is None else json.loads(d["payload"])
            out.append(d)
        return out

    def stored_payloads(self, batch_id: int) -> list[tuple[int, bytes | None]]:
        """``(event id, payload bytes exactly as stored)`` for the batch's delivered events, in
        the document's order (an unwrap delivery sends these bytes, never a re-encoding)."""
        rows = self._read("SELECT id, payload FROM events WHERE batch_id=? AND excluded_at IS NULL ORDER BY id",
                          (batch_id,))
        return [(int(r["id"]), None if r["payload"] is None else r["payload"].encode("utf-8")) for r in rows]

    def document(self, batch_id: int, delivery_id: str, attempt: int) -> dict:
        """The delivery document of §6.4 (identical for every destination type)."""
        c = self._conn()
        b = c.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if b is None:
            raise KeyError(batch_id)
        events, meta = self._frozen(c, batch_id)
        types: dict[str, int] = {}
        for m in meta:
            if m["event_type"]:
                types[m["event_type"]] = types.get(m["event_type"], 0) + 1
        n = len(events)
        detail = ", ".join(f"{t} x{k}" for t, k in sorted(types.items()))
        summary = f"{n} {b['source']} event(s) for {b['key']}" + (f" ({detail})" if detail else "")
        return {"source": b["source"], "route": b["route"], "key": b["key"], "batch_id": b["id"],
                "delivery_id": delivery_id, "attempt": attempt, "count": n, "first_at": b["first_at"],
                "last_at": b["last_at"], "event_type": meta[-1]["event_type"] if meta else "",
                "event_types": dict(sorted(types.items())), "summary": summary,
                "content_hash": b["content_hash"] or content_hash(events, meta),
                "events": events, "event_meta": meta}

    def destination_ok_between(self, destination: str, after: float, upto: float, *,
                               exclude_batch: int | None = None) -> bool:
        """Did ``destination`` accept any other batch in ``(after, upto]``? (The poison rule's
        evidence that a failure was the batch's, not the destination's.)"""
        row = self._conn().execute(
            "SELECT 1 FROM attempts WHERE destination=? AND outcome='ok' AND batch_id != ? AND finished_at > ?"
            " AND finished_at <= ? AND started_at >= ? AND started_at <= ? LIMIT 1",
            (destination, -1 if exclude_batch is None else int(exclude_batch), float(after), float(upto),
             float(after) - 86400.0, float(upto))).fetchone()
        return row is not None

    def attempts(self, batch_id: int) -> list[dict]:
        return [dict(r) for r in self._read("SELECT * FROM attempts WHERE batch_id=? ORDER BY id", (batch_id,))]

    def list(self, state: str | None = None, *, source: str | None = None, destination: str | None = None,
             limit: int = 50) -> list[dict]:
        q = ("SELECT id, route, source, destination, key, state, first_at, last_at, count, bytes, attempts,"
             " next_attempt_at, last_error, last_class, delivered_at, result FROM batches WHERE 1=1")
        args: list = []
        for col, v in (("state", state), ("source", source), ("destination", destination)):
            if v:
                q += f" AND {col}=?"
                args.append(v)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        return [dict(r) for r in self._read(q, args)]

    def show(self, batch_id: int, *, events: bool = False, payload: bool = False,
             attempts: bool = False) -> dict | None:
        b = self.batch(batch_id)
        if b is None:
            return None
        if events or payload:
            b["events"] = self.events(batch_id, include_excluded=True, payload=payload)
        if attempts:
            b["attempts"] = self.attempts(batch_id)
        return b

    def export(self, *, state: str | None = None, since: float | None = None) -> Iterator[dict]:
        """Batches with their events (raw base64) and attempts, one dict per batch (JSONL)."""
        q, args = "SELECT id FROM batches WHERE 1=1", []
        if state:
            q += " AND state=?"
            args.append(state)
        if since is not None:
            q += " AND first_at >= ?"
            args.append(float(since))
        for (bid,) in self._read(q + " ORDER BY id", args):
            b = self.batch(bid)
            if b is None:
                continue
            evs = []
            for r in self._read("SELECT * FROM events WHERE batch_id=? ORDER BY id", (bid,)):
                d = dict(r)
                d["payload"] = None if d["payload"] is None else json.loads(d["payload"])
                d["headers"] = None if d["headers"] is None else json.loads(d["headers"])
                d["raw"] = None if d["raw"] is None else base64.b64encode(bytes(d["raw"])).decode("ascii")
                evs.append(d)
            b["events"] = evs
            b["attempts"] = self.attempts(bid)
            yield b

    def status(self, now: float | None = None) -> dict:
        """The store's part of ``hookspool status`` / ``/healthz`` (ARCHITECTURE §11.1)."""
        now = self.now() if now is None else now
        c = self._conn()
        counts = {s: 0 for s in STATES}
        counts["quarantined"] = 0
        for r in c.execute("SELECT state, destination = ? AS q, COUNT(*) n FROM batches GROUP BY state, q",
                           (QUARANTINE,)):
            if r["state"] == "dead" and r["q"]:
                counts["quarantined"] += r["n"]
            else:
                counts[r["state"]] += r["n"]
        row = c.execute("SELECT MIN(first_at) FROM batches WHERE state IN ('open', 'sealed', 'leased', 'retry')"
                        ).fetchone()
        oldest = None if row[0] is None else round(now - row[0], 1)
        meta = {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM meta")}

        def age(k):
            v = meta.get(k)
            return None if v is None else round(now - float(v), 1)
        hb = age("dispatcher_heartbeat_at")
        dests: dict[str, dict] = {}
        for r in c.execute("SELECT destination, MIN(first_at) m FROM batches WHERE state IN"
                           " ('open', 'sealed', 'leased', 'retry') GROUP BY destination"):
            dests[r["destination"]] = {"state": "closed", "oldest_pending_age_s": round(now - r["m"], 1)}
        for d in c.execute("SELECT * FROM destinations"):
            e = dests.setdefault(d["name"], {"oldest_pending_age_s": None})
            e["state"] = d["state"]
            if d["state"] in ("open", "half_open"):
                e.update(opened_at=d["opened_at"], cooldown_s=d["cooldown_s"])
            if d["last_error"]:
                e["last_error"] = d["last_error"]
        sources: dict[str, dict] = {}
        for k in meta:
            if k.startswith("source:") and k.endswith(":last_event_at"):
                sources[k[len("source:"):-len(":last_event_at")]] = {"last_event_age_s": age(k)}
        for name, s in (self.config.get("sources") or {}).items():
            e = sources.setdefault(name, {"last_event_age_s": None})
            exp = (s or {}).get("expect_every_s") if isinstance(s, dict) else None
            if exp:
                e["expected_every_s"] = exp
                ref = e["last_event_age_s"]
                e["silent"] = ref is None or ref > float(exp)
        degraded = []
        if counts["dead"]:
            degraded.append(f"{counts['dead']} dead batch(es)")
        if counts["quarantined"]:
            degraded.append(f"{counts['quarantined']} quarantined request(s)")
        for name, d in sorted(dests.items()):
            if d.get("state") in ("open", "half_open"):
                degraded.append(f"destination {name} breaker {d['state']}")
            elif d.get("state") == "paused":
                degraded.append(f"destination {name} paused")
        for name, s in sorted(sources.items()):
            if s.get("silent"):
                degraded.append(f"source {name} silent")
        if hb is None or hb > HEARTBEAT_STALE_S:
            degraded.append("dispatcher heartbeat stale" if hb is not None else "dispatcher has not run")
        spool_bytes = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                spool_bytes += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        try:
            st = os.statvfs(os.path.dirname(os.path.abspath(self.path)))
            free = st.f_bavail * st.f_frsize
        except OSError:
            free = None
        issues_open = c.execute("SELECT COUNT(*) FROM issues WHERE state IN ('open', 'acked')").fetchone()[0]
        return {"ok": True, "degraded": degraded, "schema": self.schema_version(),
                "dispatcher": {"alive": hb is not None and hb <= HEARTBEAT_STALE_S, "heartbeat_age_s": hb,
                               "pid": int(meta["dispatcher_pid"]) if meta.get("dispatcher_pid") else None},
                "spool": {"last_enqueue_age_s": age("last_enqueue_at")},
                "batches": counts, "oldest_pending_age_s": oldest, "destinations": dests, "sources": sources,
                "issues_open": issues_open, "spool_bytes": spool_bytes, "disk_free_bytes": free}
