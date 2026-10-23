"""The intake server (contract C4; ARCHITECTURE §9, §10, §11).

``hookspool intake`` is a ``ThreadingHTTPServer`` on loopback behind the reverse proxy,
one path per source, ``/hooks/<name>``. Each request runs, in this order and no other:

    body cap (411 on chunked or a missing length, 413 over the cap)
    → challenge, only for a ``challenge_unauthenticated`` source (secret-free, constant)
    → verify (401, empty body)
    → challenge (verified handshakes)
    → ignore → payload → key, dedup_id, event_type, attrs → route resolution (and tee routes)
    → enqueue every copy (one COMMIT) → 200 {"status": "queued" | "duplicate" | "replayed" | "ignored"}

Anything that raises after ``verify`` (plugin code, an event the store refuses, route
resolution) quarantines the request: the raw body and its headers minus the source's
``auth_headers`` are committed as a ``dead`` quarantine batch and the answer is
``202 {"status": "quarantined"}``. No verified request is ever answered 5xx because of
our own code; the only 5xx is 503 (``Retry-After: 60``) when the spool cannot take the
write (``SpoolError``) or free disk is below ``limits.min_free_bytes``. There is no 429:
the proxy rate-limits.

``GET /healthz`` and ``GET /metrics`` are served on the same port (§11). Logs carry ids,
names, sizes, codes and durations only: never bodies, headers, secrets or query strings.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import signal
import socket
import socketserver
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from . import config as _config
from . import logs as _logs
from .plugin import (ALWAYS_STRIPPED, PluginError, RawRequest, Request, Response, SecretError, Source,
                     load_source, parse_capture)
from .store import InvalidEvent, SchemaError, SpoolError, Store

log = logging.getLogger("hookspool.intake")

RETRY_AFTER_S = 60
FREE_CACHE_S = 5.0
PROBE_EVERY_S = 30.0
RELOAD_CHECK_S = 5.0
REQUEST_TIMEOUT_S = 30.0
BODY_METHODS = ("POST", "PUT", "PATCH")
REJECT_REASONS = ("unverified", "too_large", "chunked", "spool_full", "busy", "ignored")


class NoDestination(Exception):
    """Neither a route, the source config nor the plugin names a destination."""


# ---- stages (shared by the server, `hookspool source test` and quarantine replay) -----------------------
@dataclass
class Staged:
    """The result of running a request through a source's stages, before enqueue."""
    kind: str                     # challenge | unverified | verify_error | ignored | ready | error
    response: Response | None = None
    reason: str = ""              # ignore reason
    stage: str = ""               # where an error was raised
    error: str = ""               # exception class name (never its message)
    fields: dict = field(default_factory=dict)


def classify(src: Source, req: Request, cfg: dict) -> Staged:
    """ignore → payload → key/dedup_id/event_type/attrs → route resolution (no verify)."""
    stage = "ignore"
    try:
        reason = src.ignore(req)
        if reason is not None:
            return Staged("ignored", reason=reason)
        stage = "payload"
        payload = src.payload(req)
        stage = "key"
        key = src.key(payload, req)
        if isinstance(key, int) and not isinstance(key, bool):
            key = str(key)
        stage = "dedup_id"
        dd = src.dedup_id(payload, req)
        stage = "event_type"
        et = src.event_type(payload, req)
        stage = "attrs"
        attrs = src.attrs(payload, req)
        stage = "route"
        default_action = src.action(payload, req)
        route, dest = _config.resolve_route(cfg, src.name, attrs, default_action)
        if not dest:
            raise NoDestination(src.name)
        tees = _config.resolve_tees(cfg, src.name, attrs, dest)
    except Exception as e:  # noqa: BLE001 - every plugin failure quarantines
        return Staged("error", stage=stage, error=e.__class__.__name__)
    return Staged("ready", fields={"payload": payload, "key": key, "dedup_id": dd, "event_type": et,
                                   "attrs": attrs, "route": route, "destination": dest, "tees": tees})


def run_stages(src: Source, req: Request, cfg: dict) -> Staged:
    """Unauthenticated challenge → verify → challenge → classify. Raises SecretError when a
    secret the verifier needs cannot be read (the caller answers 503)."""
    if src.challenge_unauthenticated and src.has_challenge:
        try:
            r = src.challenge(req)
        except Exception as e:  # noqa: BLE001 - a failing secret-free handshake falls through to verify
            log.warning("unauthenticated challenge raised; continuing to verify",
                        extra=_logs.fields(source=src.name, reason=e.__class__.__name__))
            r = None
        if r is not None:
            return Staged("challenge", response=r)
    try:
        ok = src.verify(req)
    except SecretError:
        raise
    except Exception as e:  # noqa: BLE001
        return Staged("verify_error", error=e.__class__.__name__)
    if not ok:
        return Staged("unverified")
    if not src.challenge_unauthenticated and src.has_challenge:
        try:
            r = src.challenge(req)
        except Exception as e:  # noqa: BLE001
            return Staged("error", stage="challenge", error=e.__class__.__name__)
        if r is not None:
            return Staged("challenge", response=r)
    return classify(src, req, cfg)


def reclassifier(cfg: dict, loader: Callable[[str, str | None], Any] = _config.default_plugin_loader,
                 now: Callable[[], float] = time.time) -> Callable[[str, bytes, dict], dict | None]:
    """``fn(source, raw, headers)`` for ``Store.reclassify_quarantine``: re-run classification
    of a stored request through the (fixed) plugin. Verification is not repeated (the
    request was verified when it was quarantined). Raises if the plugin still fails."""
    cache: dict[str, Source] = {}

    def fn(source: str, raw: bytes, headers: dict) -> dict | None:
        if source not in cache:
            if source not in (cfg.get("sources") or {}):
                raise PluginError(f"source {source} is not configured")
            cache[source] = load_source(source, _config.source_cfg(cfg, source), loader)
        src = cache[source]
        hs = [(k, v) for k, v in (headers or {}).items() if not k.startswith(":")]
        req = Request((headers or {}).get(":method", "POST"), (headers or {}).get(":path", f"/hooks/{source}"),
                      hs, raw, now=now(), config=src.public_config())
        st = classify(src, req, cfg)
        if st.kind == "ignored":
            return None
        if st.kind != "ready":
            raise PluginError(f"source {source}: {st.error} in {st.stage}")
        f = st.fields
        return {"source": source, "key": f["key"], "action": f["destination"], "payload": f["payload"],
                "route": f["route"], "event_type": f["event_type"], "dedup_id": f["dedup_id"],
                "headers": src.kept_headers(req), "tees": f["tees"]}
    return fn


# ---- counters ---------------------------------------------------------------------------------------------
class Counters:
    """Per-process intake counters (reset on restart; Prometheus ``rate()`` copes)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.received: dict[str, int] = {}
        self.duplicate: dict[str, int] = {}
        self.quarantined: dict[str, int] = {}
        self.rejected: dict[tuple[str, str], int] = {}

    def inc(self, table: str, k: Any) -> None:
        with self._lock:
            d = getattr(self, table)
            d[k] = d.get(k, 0) + 1

    def snapshot(self) -> dict:
        with self._lock:
            return {"received": dict(self.received), "duplicate": dict(self.duplicate),
                    "quarantined": dict(self.quarantined), "rejected": dict(self.rejected)}


# ---- the intake -------------------------------------------------------------------------------------------
@dataclass
class _Snapshot:
    cfg: dict
    sources: dict[str, Source]
    caps: dict[str, int]


def _build(cfg: dict, loader) -> _Snapshot:
    sources: dict[str, Source] = {}
    caps: dict[str, int] = {}
    for name, s in (cfg.get("sources") or {}).items():
        src = load_source(name, s if isinstance(s, dict) else {}, loader)
        sources[name] = src
        caps[name] = _config.source_max_body_bytes(cfg, name, plugin_span=src.span)
    return _Snapshot(cfg, sources, caps)


def _empty(status: int, headers: dict | None = None) -> Response:
    return Response(status, b"", dict(headers or {}))


def _busy(e: Exception) -> bool:
    t = str(e).lower()
    return "locked" in t or "busy" in t


class Intake:
    """The request pipeline, independent of the socket layer (``handle`` is what the HTTP
    handler calls; tests drive it directly or over a real socket)."""

    def __init__(self, cfg: dict, store: Store | None = None, *, now: Callable[[], float] = time.time,
                 loader: Callable[[str, str | None], Any] = _config.default_plugin_loader,
                 statvfs: Callable[[str], Any] = os.statvfs, config_path: str | None = None,
                 cli_home: str | None = None):
        self.now = now
        self.loader = loader
        self.statvfs = statvfs
        self.config_path = config_path
        self.cli_home = cli_home      # --home: reloads must expand with the same home as startup
        self._snap = _build(cfg, loader)
        if store is None:
            store = Store(_config.db_path(cfg), cfg, now=now,
                          busy_timeout_s=float((cfg.get("intake") or {}).get("busy_timeout_s") or 10))
        self.store = store
        self.store.span_for = self._span_for
        self.counters = Counters()
        self._lock = threading.Lock()
        self._free: tuple[float, int | None] | None = None      # (checked_at, free bytes)
        self._last_write_ok: float | None = None
        self._probe_at: float | None = None
        self._probe_ok: bool | None = None
        self._reload_checked = time.monotonic()
        self._mtime = self._stat_mtime()

    # -- config
    @property
    def cfg(self) -> dict:
        return self._snap.cfg

    def _span_for(self, source: str) -> str | None:
        s = self._snap.sources.get(source)
        return s.span if s is not None else None

    def _stat_mtime(self) -> float | None:
        if not self.config_path:
            return None
        try:
            return os.stat(self.config_path).st_mtime_ns
        except OSError:
            return None

    def maybe_reload(self, *, force: bool = False) -> bool:
        """Reload the config when its mtime changed (checked at most every 5 s). An invalid
        file keeps the running config. Returns True when a new config was applied."""
        if not self.config_path:
            return False
        mono = time.monotonic()
        if not force and mono - self._reload_checked < RELOAD_CHECK_S:
            return False
        with self._lock:
            self._reload_checked = mono
            m = self._stat_mtime()
            if m is None or m == self._mtime:
                return False
            self._mtime = m
            try:
                cfg = _config.load_config(self.config_path, cli_home=self.cli_home)
                rep = _config.check(cfg, load_plugin=self.loader, check_secrets=False)
                if not rep.ok:
                    raise _config.ConfigError(f"{len(rep.errors)} error(s)")
                snap = _build(cfg, self.loader)
            except Exception as e:  # noqa: BLE001 - keep serving with the previous config
                log.error("config reload failed; keeping the previous config",
                          extra=_logs.fields(reason=e.__class__.__name__))
                return False
            self._snap = snap
            self.store.config = cfg
            log.info("config reloaded", extra=_logs.fields(reason="mtime"))
            return True

    # -- limits
    def _min_free(self) -> int:
        return int((self.cfg.get("limits") or {}).get("min_free_bytes") or 0)

    def disk_free(self, now: float | None = None) -> int | None:
        """Free bytes on the spool's filesystem, cached 5 s."""
        now = self.now() if now is None else now
        f = self._free
        if f is not None and 0 <= now - f[0] < FREE_CACHE_S:
            return f[1]
        try:
            st = self.statvfs(os.path.dirname(os.path.abspath(self.store.path)))
            free = int(st.f_bavail * st.f_frsize)
        except OSError:
            free = None
        self._free = (now, free)
        return free

    def low_disk(self, now: float | None = None) -> bool:
        free = self.disk_free(now)
        return free is not None and free < self._min_free()

    # -- the pipeline
    def handle(self, method: str, target: str, headers: Iterable[tuple[str, str]], rfile,
               peer: str = "") -> Response:
        try:
            return self._handle(method.upper(), target, list(headers), rfile, peer)
        except Exception as e:  # noqa: BLE001 - our own bug before verify: never a 500
            log.error("intake error", extra=_logs.fields(reason=e.__class__.__name__))
            return _empty(503, {"Retry-After": str(RETRY_AFTER_S)})

    def _handle(self, method: str, target: str, headers: list[tuple[str, str]], rfile, peer: str) -> Response:
        self.maybe_reload()
        snap = self._snap
        path = urlsplit(target).path
        if path in ("/healthz", "/metrics"):
            if method not in ("GET", "HEAD"):
                return _empty(405, {"Allow": "GET, HEAD", "Connection": "close"})    # a body is not read
            if path == "/healthz":
                code, doc = self.health()
                return Response.json(code, doc)
            return Response(200, self.metrics_text().encode("utf-8"),
                            {"Content-Type": "text/plain; version=0.0.4; charset=utf-8"})
        name = path[len("/hooks/"):] if path.startswith("/hooks/") else ""
        src = snap.sources.get(name) if name and "/" not in name else None
        if src is None:
            return _empty(404, {"Connection": "close"})
        hmap: dict[str, str] = {}
        for k, v in headers:
            hmap.setdefault(k.lower(), v)
        remote = peer
        if _loopback(peer) and hmap.get("x-forwarded-for"):
            remote = hmap["x-forwarded-for"].split(",")[0].strip()[:64]

        # body cap: before any plugin code
        if hmap.get("transfer-encoding", "").strip():
            self.counters.inc("rejected", (name, "chunked"))
            log.warning("rejected: chunked request", extra=_logs.fields(source=name, status=411))
            return _empty(411, {"Connection": "close"})
        cl = hmap.get("content-length")
        if cl is None or cl.strip() == "":
            if method in BODY_METHODS:
                self.counters.inc("rejected", (name, "chunked"))
                log.warning("rejected: no Content-Length", extra=_logs.fields(source=name, status=411))
                return _empty(411, {"Connection": "close"})
            length = 0
        else:
            cl = cl.strip()
            if not (cl.isascii() and cl.isdigit()):
                return _empty(400, {"Connection": "close"})
            length = int(cl)
        cap = snap.caps[name]
        if length > cap:
            self.counters.inc("rejected", (name, "too_large"))
            log.warning("rejected: body over the cap",
                        extra=_logs.fields(source=name, status=413, bytes=length))
            return _empty(413, {"Connection": "close"})
        body = rfile.read(length) if length else b""
        if len(body) != length:
            return _empty(400, {"Connection": "close"})
        req = Request(method, target, headers, body, remote_addr=remote, now=self.now(),
                      config=src.public_config())
        return self._process(snap, src, req)

    def _process(self, snap: _Snapshot, src: Source, req: Request) -> Response:
        name = src.name
        try:
            st = run_stages(src, req, snap.cfg)
        except SecretError:
            log.error("secret unavailable; cannot verify", extra=_logs.fields(source=name, status=503))
            return _empty(503, {"Retry-After": str(RETRY_AFTER_S)})
        if st.kind == "challenge":
            log.info("handshake answered", extra=_logs.fields(source=name, status=st.response.status))
            return st.response
        if st.kind == "verify_error":
            log.error("verify raised", extra=_logs.fields(source=name, reason=st.error, status=503))
            return _empty(503, {"Retry-After": str(RETRY_AFTER_S)})
        if st.kind == "unverified":
            self.counters.inc("rejected", (name, "unverified"))
            log.warning("unverified request", extra=_logs.fields(source=name, status=401, bytes=len(req.body),
                                                                 reason=f"remote {req.remote_addr}"))
            return _empty(401)
        if st.kind == "ignored":
            self.counters.inc("rejected", (name, "ignored"))
            log.info("ignored", extra=_logs.fields(source=name, reason=st.reason[:80]))
            return Response.json(200, {"status": "ignored"})
        if st.kind == "error":
            return self._quarantine(src, req, f"plugin error: {st.error} in {st.stage}")
        f = st.fields
        if self.low_disk():
            return self._unavailable(name, "spool_full", "free disk below limits.min_free_bytes")
        try:
            r = self.store.enqueue(name, f["key"], f["destination"], f["payload"], route=f["route"],
                                   event_type=f["event_type"], dedup_id=f["dedup_id"], raw=req.body,
                                   headers=src.kept_headers(req), tees=f["tees"])
        except InvalidEvent as e:
            return self._quarantine(src, req, f"plugin error: {e.__class__.__name__} in enqueue")
        except SpoolError as e:
            return self._unavailable(name, "busy" if _busy(e) else "spool_full", "spool write failed")
        except Exception as e:  # noqa: BLE001 - never a 500 for a verified request
            return self._quarantine(src, req, f"plugin error: {e.__class__.__name__} in enqueue")
        self._last_write_ok = self.now()
        status = "replayed" if r.replayed else "duplicate" if r.duplicate else "queued"
        self.counters.inc("received", name)
        if r.duplicate:
            self.counters.inc("duplicate", name)
        log.info(status, extra=_logs.fields(source=name, route=f["route"], destination=f["destination"],
                                            batch=r.batch_id, event=r.event_id, bytes=len(req.body)))
        for (route, dest), t in zip(f["tees"], r.tees):         # empty for a duplicate or a replay
            log.info(status, extra=_logs.fields(source=name, route=route, destination=dest, batch=t.batch_id,
                                                event=t.event_id, bytes=len(req.body), tee=True))
        return Response.json(200, {"status": status})

    def _unavailable(self, name: str, reason: str, msg: str) -> Response:
        self.counters.inc("rejected", (name, reason))
        log.error(msg, extra=_logs.fields(source=name, status=503, reason=reason))
        return _empty(503, {"Retry-After": str(RETRY_AFTER_S)})

    def _quarantine(self, src: Source, req: Request, error: str) -> Response:
        name = src.name
        if self.low_disk():
            return self._unavailable(name, "spool_full", "free disk below limits.min_free_bytes")
        try:
            eid, bid = self.store.quarantine(name, req.body, src.quarantine_headers(req), error,
                                             auth_headers=tuple(src.auth_headers) + ALWAYS_STRIPPED)
        except SpoolError as e:
            return self._unavailable(name, "busy" if _busy(e) else "spool_full", "quarantine write failed")
        self._last_write_ok = self.now()
        self.counters.inc("quarantined", name)
        log.error("quarantined", extra=_logs.fields(source=name, batch=bid, event=eid, bytes=len(req.body),
                                                    reason=error, status=202))
        return Response.json(202, {"status": "quarantined"})

    # -- health (§11.1)
    def _writable(self, now: float, last_enqueue_age: float | None) -> tuple[bool, float | None]:
        proofs = [t for t in (self._last_write_ok,
                              None if last_enqueue_age is None else now - last_enqueue_age) if t is not None]
        last = max(proofs) if proofs else None
        if last is not None and now - last <= PROBE_EVERY_S:
            return True, round(now - last, 1)
        with self._lock:
            if self._probe_at is not None and 0 <= now - self._probe_at < PROBE_EVERY_S:
                age = None if last is None else round(now - last, 1)
                return bool(self._probe_ok), age
            self._probe_at = now
            try:
                self.store.meta_set({"intake_probe_at": repr(now)})
                self._probe_ok = True
                self._last_write_ok = now
                return True, 0.0
            except SpoolError:
                self._probe_ok = False
                return False, None if last is None else round(now - last, 1)

    def health(self) -> tuple[int, dict]:
        now = self.now()
        try:
            doc = self.store.status(now)
        except Exception as e:  # noqa: BLE001
            log.error("status failed", extra=_logs.fields(reason=e.__class__.__name__))
            return 503, {"ok": False, "degraded": ["spool unreadable"]}
        hard: list[str] = []
        ok_w, age = self._writable(now, (doc.get("spool") or {}).get("last_enqueue_age_s"))
        doc.setdefault("spool", {})["writable_checked_age_s"] = age
        if not ok_w:
            hard.append("spool unwritable")
        free = self.disk_free(now)
        if free is not None:
            doc["disk_free_bytes"] = free
            if free < self._min_free():
                hard.append("disk free below limits.min_free_bytes")
        doc["degraded"] = hard + list(doc.get("degraded") or [])
        doc["ok"] = not hard
        return (200 if not hard else 503), doc

    # -- metrics (§11.2)
    def metrics_text(self) -> str:
        now = self.now()
        out: list[str] = []
        c = self.counters.snapshot()

        def series(name: str, kind: str, rows: Iterable[tuple[dict, float]]) -> None:
            out.append(f"# TYPE {name} {kind}")
            for labels, v in rows:
                lab = ",".join(f'{k}="{_esc(str(val))}"' for k, val in labels.items())
                out.append(f"{name}{{{lab}}} {_num(v)}" if lab else f"{name} {_num(v)}")

        names = sorted(self._snap.sources)
        series("hookspool_events_received_total", "counter",
               [({"source": s}, c["received"].get(s, 0)) for s in names])
        series("hookspool_events_duplicate_total", "counter",
               [({"source": s}, c["duplicate"].get(s, 0)) for s in names])
        series("hookspool_events_quarantined_total", "counter",
               [({"source": s}, c["quarantined"].get(s, 0)) for s in names])
        rej = [({"source": s, "reason": r}, n) for (s, r), n in sorted(c["rejected"].items())]
        series("hookspool_requests_rejected_total", "counter", rej)
        try:
            doc = self.store.status(now)
            tables = _table_figures(self.store, now)
        except Exception as e:  # noqa: BLE001 - counters still render
            log.error("metrics: spool unreadable", extra=_logs.fields(reason=e.__class__.__name__))
            return "\n".join(out) + "\n"
        series("hookspool_attempts_30d", "gauge",
               [({"destination": d, "outcome": o, "klass": k}, n) for d, o, k, n in tables["attempts"]])
        series("hookspool_batches_delivered_7d", "gauge",
               [({"destination": d}, n) for d, n in tables["delivered"]])
        series("hookspool_attempt_duration_seconds_sum_30d", "gauge",
               [({"destination": d}, s) for d, s, _ in tables["durations"]])
        series("hookspool_attempt_duration_seconds_count_30d", "gauge",
               [({"destination": d}, n) for d, _, n in tables["durations"]])
        series("hookspool_batches", "gauge", [({"state": k}, v) for k, v in sorted(doc["batches"].items())])
        _maybe(series, "hookspool_oldest_pending_age_seconds", doc.get("oldest_pending_age_s"))
        codes = {"closed": 0, "half_open": 1, "open": 2, "paused": 3}
        dests = sorted((doc.get("destinations") or {}).items())
        series("hookspool_destination_state", "gauge",
               [({"destination": d}, codes.get(v.get("state"), 0)) for d, v in dests])
        series("hookspool_destination_oldest_pending_age_seconds", "gauge",
               [({"destination": d}, v["oldest_pending_age_s"]) for d, v in dests
                if v.get("oldest_pending_age_s") is not None])
        _maybe(series, "hookspool_issues_open", doc.get("issues_open"))
        _maybe(series, "hookspool_spool_bytes", doc.get("spool_bytes"))
        _maybe(series, "hookspool_disk_free_bytes", self.disk_free(now))
        _maybe(series, "hookspool_dispatcher_heartbeat_age_seconds", (doc.get("dispatcher") or {}).get("heartbeat_age_s"))
        series("hookspool_source_last_event_age_seconds", "gauge",
               [({"source": s}, v["last_event_age_s"]) for s, v in sorted((doc.get("sources") or {}).items())
                if v.get("last_event_age_s") is not None])
        return "\n".join(out) + "\n"


def _maybe(series, name: str, v: Any) -> None:
    if v is not None:
        series(name, "gauge", [({}, v)])


def _num(v: Any) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    f = float(v)
    return str(int(f)) if f.is_integer() and abs(f) < 1e15 else repr(f)


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n")


def _table_figures(st: Store, now: float) -> dict:
    """Dispatcher figures as gauges over the retention window (§11.2: pruned tables are not
    monotonic, so these are not counters)."""
    d30, d7 = now - 30 * 86400.0, now - 7 * 86400.0
    attempts = [(r[0], r[1] or "pending", r[2] or "", r[3]) for r in st._read(
        "SELECT destination, outcome, klass, COUNT(*) FROM attempts WHERE started_at >= ?"
        " GROUP BY destination, outcome, klass ORDER BY destination, outcome, klass", (d30,))]
    delivered = [(r[0], r[1]) for r in st._read(
        "SELECT destination, COUNT(*) FROM batches WHERE state='delivered' AND delivered_at >= ?"
        " GROUP BY destination ORDER BY destination", (d7,))]
    durations = [(r[0], (r[1] or 0) / 1000.0, r[2]) for r in st._read(
        "SELECT destination, SUM(duration_ms), COUNT(duration_ms) FROM attempts WHERE started_at >= ?"
        " AND duration_ms IS NOT NULL GROUP BY destination ORDER BY destination", (d30,))]
    return {"attempts": attempts, "delivered": delivered, "durations": durations}


def _loopback(addr: str) -> bool:
    if addr in ("", "unix"):
        return True
    try:
        return ipaddress.ip_address(addr.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


# ---- HTTP layer ----------------------------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server_version = "hookspool"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = REQUEST_TIMEOUT_S

    def _do(self) -> None:
        ca = self.client_address
        peer = ca[0] if isinstance(ca, tuple) and ca else "unix"
        resp = self.server.intake.handle(self.command, self.path, self.headers.items(), self.rfile, peer)
        self.send_response(resp.status)
        for k, v in resp.headers.items():
            if k.lower() not in ("content-length", "connection"):
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(resp.body)))
        if resp.headers.get("Connection", "").lower() == "close":
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if self.command != "HEAD" and resp.body:
            self.wfile.write(resp.body)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _do

    def log_message(self, format: str, *args: Any) -> None:  # the request line carries query strings
        pass

    def log_error(self, format: str, *args: Any) -> None:
        log.warning("http protocol error")


class _BoundedMixIn(socketserver.ThreadingMixIn):
    daemon_threads = True
    block_on_close = False

    def process_request(self, request, client_address):
        self._slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):  # never print tracebacks (they may carry data)
        log.error("connection error", extra=_logs.fields(reason=sys.exc_info()[0].__name__
                                                         if sys.exc_info()[0] else None))


class IntakeServer(_BoundedMixIn, HTTPServer):
    allow_reuse_address = True

    def __init__(self, addr, intake: Intake, threads: int = 16):
        self.intake = intake
        self._slots = threading.BoundedSemaphore(max(1, int(threads)))
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        super().__init__(addr, _Handler)

    def server_bind(self):  # skip HTTPServer's getfqdn() lookup
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class UnixIntakeServer(_BoundedMixIn, socketserver.UnixStreamServer):
    def __init__(self, path: str, intake: Intake, threads: int = 16):
        self.intake = intake
        self._slots = threading.BoundedSemaphore(max(1, int(threads)))
        if os.path.exists(path):
            os.unlink(path)
        super().__init__(path, _Handler)
        os.chmod(path, 0o660)


def parse_listen(listen: str) -> tuple[str, Any]:
    """``("tcp", (host, port))`` or ``("unix", path)``. Only loopback addresses are allowed."""
    if listen.startswith("unix:"):
        return "unix", listen[5:]
    host, sep, port = listen.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"intake.listen {listen!r}: expected host:port or unix:/path")
    host = host.strip("[]")
    if host == "localhost":
        host = "127.0.0.1"
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError(f"intake.listen {listen!r}: the intake binds loopback only")
    except ValueError as e:
        raise ValueError(str(e) if "loopback" in str(e) else f"intake.listen {listen!r}: not an IP address") from None
    return "tcp", (host, int(port))


def make_server(intake: Intake, listen: str | None = None):
    listen = listen or str((intake.cfg.get("intake") or {}).get("listen") or "127.0.0.1:8650")
    threads = int((intake.cfg.get("intake") or {}).get("threads") or 16)
    kind, addr = parse_listen(listen)
    if kind == "unix":
        return UnixIntakeServer(addr, intake, threads)
    return IntakeServer(addr, intake, threads)


# ---- `hookspool source test` ------------------------------------------------------------------------------------
def source_test(cfg: dict, name: str, raw: RawRequest, *, now: float | None = None,
                loader: Callable[[str, str | None], Any] = _config.default_plugin_loader) -> dict:
    """Run a captured request through a source's stages offline. Never enqueues; the
    result carries no payload, only what the stages decided."""
    if name not in (cfg.get("sources") or {}):
        raise PluginError(f"source {name} is not configured")
    src = load_source(name, _config.source_cfg(cfg, name), loader)
    t = float(raw.meta["now"]) if now is None and raw.meta.get("now") else (time.time() if now is None else now)
    out: dict[str, Any] = {"source": name, "method": raw.method, "bytes": len(raw.body), "now": t}
    cap = _config.source_max_body_bytes(cfg, name, plugin_span=src.span)
    if len(raw.body) > cap:
        out.update(result="too_large", status=413)
        return out
    req = Request(raw.method, raw.target, raw.headers, raw.body, now=t, config=src.public_config(),
                  remote_addr="capture")
    try:
        st = run_stages(src, req, cfg)
    except SecretError as e:
        out.update(result="secret_unavailable", status=503, error=str(e))
        return out
    if st.kind == "challenge":
        out.update(result="challenge", status=st.response.status, response_bytes=len(st.response.body))
    elif st.kind == "unverified":
        out.update(result="unverified", status=401)
    elif st.kind == "verify_error":
        out.update(result="verify_error", status=503, error=st.error)
    elif st.kind == "ignored":
        out.update(result="ignored", status=200, reason=st.reason)
    elif st.kind == "error":
        out.update(result="quarantined", status=202, stage=st.stage, error=st.error)
    else:
        f = st.fields
        payload = f["payload"]
        out.update(result="queued", status=200, key=f["key"], dedup_id=f["dedup_id"], event_type=f["event_type"],
                   attrs=f["attrs"], route=f["route"], destination=f["destination"],
                   tees=[{"route": r, "destination": d} for r, d in f["tees"]],
                   payload_bytes=len(json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")),
                   kept_headers=sorted((src.kept_headers(req) or {}).keys()))
    return out


# ---- CLI entry (`hookspool intake`, `hookspool metrics`, `hookspool source test`) --------------------------------
def cli(a, cfg: dict) -> int:
    db = _config.db_path(cfg, getattr(a, "db", None), getattr(a, "home", None))
    if a.cmd == "source":
        try:
            with open(a.request, "rb") as f:
                raw = parse_capture(f.read())
        except (OSError, ValueError, UnicodeDecodeError) as e:
            print(f"hookspool: cannot read capture {a.request}: {e.__class__.__name__}", file=sys.stderr)
            return 2
        try:
            out = source_test(cfg, a.name, raw, now=getattr(a, "now", None))
        except (PluginError, ImportError) as e:
            print(f"hookspool: {e.__class__.__name__}: {e}", file=sys.stderr)
            return 2
        print(json.dumps(out, indent=1, sort_keys=True, default=str))
        return 0 if out["result"] in ("queued", "ignored", "challenge") else 1

    store = Store(db, cfg, busy_timeout_s=float((cfg.get("intake") or {}).get("busy_timeout_s") or 10))
    try:
        store.check_schema()
    except SchemaError as e:
        print(f"hookspool: {e}", file=sys.stderr)
        return 2
    try:
        ik = Intake(cfg, store, config_path=cfg.get("_path"), cli_home=getattr(a, "home", None))
    except (PluginError, ImportError) as e:
        print(f"hookspool: cannot load source plugins: {e.__class__.__name__}: {e}", file=sys.stderr)
        return 2
    if a.cmd == "metrics":
        sys.stdout.write(ik.metrics_text())
        return 0

    _logs.setup()
    try:
        srv = make_server(ik)
    except (ValueError, OSError) as e:
        print(f"hookspool: cannot listen: {e}", file=sys.stderr)
        return 2
    stop = threading.Event()

    def _term(signum, frame):
        if not stop.is_set():
            stop.set()
            threading.Thread(target=srv.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    log.info("intake listening", extra=_logs.fields(reason=str((ik.cfg.get("intake") or {}).get("listen"))))
    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        srv.server_close()
        store.close()
    return 0
