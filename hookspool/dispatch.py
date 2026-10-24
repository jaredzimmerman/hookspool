"""Dispatcher (contract C2; ARCHITECTURE §2, §4.2, §6, §8, §11.4, §12).

The single deliverer. It holds ``<home>/dispatch.lock`` (a second instance exits 3),
applies schema migrations at startup, and every poll (``poll_s``):

1. reloads the config if its mtime changed (an invalid file keeps the old config and
   opens ``config:file``; a good one re-dues every ``config``-class retry);
2. computes ``now_eff = max(wall clock, last tick)``, so a backwards clock step makes
   time stand still instead of rewriting anything;
3. writes the heartbeat every ``heartbeat_s``;
4. recovers expired leases that no live worker of this process holds (uncounted);
5. seals every ready ``open`` batch, before any destination gate;
6. runs maintenance every 60 s (retention hourly, silent sources, dead-batch issues);
7. hands unalerted issues to the alert thread;
8. claims candidates (head of group only, oldest first) through each destination's
   gate and hands them to the worker pool.

Workers only call ``Destination.deliver``; the main thread records every outcome, so
breaker and issue updates are serialised. Every mark is fenced by the claim's
``lease_owner``: a late result from a worker that lost its lease is discarded.

Outcome classes (§6.5): ``transport`` uncounted + breaker failure; ``throttle``
uncounted, at ``Retry-After``; ``batch`` counted (``max_attempts`` -> dead);
``config`` uncounted at the backoff cap with a ``config:delivery:<dest>`` issue;
``permanent`` -> dead; ``poison`` -> dead by comparison: a ``transport`` failure while the
breaker is closed that completes ``POISON_STRIKES`` strikes (consecutive transport failures of
the batch, each after the destination accepted another batch since the previous one).

Logs carry ids, names, states, codes and durations; never payloads, secrets or
response bodies.
"""
from __future__ import annotations

import itertools
import logging
import os
import random
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Callable

from . import breaker as _breaker
from . import config as _config
from . import logs as _logs
from .destinations import build as _build_destination
from .destinations.base import DeliveryContext, Destination, Outcome, encode_body
from .store import (QUARANTINE, Attempt, InvalidEvent, LeaseLost, LockHeld, SchemaError, SpoolError, Store,
                    acquire_lock, scrub)

log = logging.getLogger("hookspool.dispatch")

DEADLINE_MARGIN_S = _config.LEASE_MARGIN_S
MAINTENANCE_S = 60.0
RETENTION_S = 3600.0
RETRY_AFTER_CAP_S = 3600.0
ALERT_RETRY_S = 60.0
STREAK_ISSUE_AT = 3
POISON_STRIKES = 5      # transport failures, each after another batch's success (§4.2)
# delivery issue reason classes that a successful attempt on the destination resolves
TRANSIENT_REASONS = ("unreachable", "http_5xx", "timeout", "script_exit", "batch")
EXIT_OK, EXIT_CONFIG, EXIT_LOCK = 0, 2, 3


def backoff_s(n: int, base: float = 15.0, cap: float = 300.0, jitter: float = 0.0) -> float:
    """``min(cap, base * 2^(n-1))`` plus ``jitter`` (a fraction, 0-0.25) of it."""
    n = max(1, int(n))
    d = cap if n > 32 else min(cap, base * 2 ** (n - 1))
    return d * (1.0 + max(0.0, float(jitter)))


def reason_class(out: Outcome, dest_type: str) -> str:
    """The issue fingerprint's reason class for a failed outcome (§11.4)."""
    if out.klass == "transport":
        if out.status is not None and 500 <= int(out.status) < 600:
            return "http_5xx"
        r = (out.reason or "").lower()
        if "timeout" in r or "timed out" in r or "deadline" in r:
            return "timeout"
        return "unreachable"
    if out.klass == "batch":
        return "script_exit" if dest_type == "script" else "batch"
    return out.klass or "unknown"


@dataclass
class _Work:
    batch: dict
    dest: Destination
    owner: str
    attempt: Attempt
    probe: bool
    started_at: float
    lease_until: float
    doc: dict
    body: bytes
    ctx: DeliveryContext
    duration_ms: float = 0.0


class Dispatcher:
    def __init__(self, store: Store | None = None, config: dict | None = None, *,
                 now: Callable[[], float] = time.time, config_path: str | None = None,
                 destination_factory: Callable[[dict, str], Destination] | None = None,
                 jitter: Callable[[], float] | None = None, lock_path: str | None = None,
                 cli_home: str | None = None):
        self._cli_home = cli_home      # --home: reloads must expand with the same home as startup
        if config is None:
            config_path = config_path or _config.config_path()
            config = _config.load_config(config_path, cli_home=cli_home)
        self._config_path = config_path if config_path is not None else config.get("_path")
        self._mtime = self._stat()
        self.config = config
        self.store = store if store is not None else Store(None, config, now=now)
        self.store.config = config
        self.wall = now
        self._factory = destination_factory or _build_destination
        self._jitter = jitter or (lambda: random.uniform(0.0, 0.25))
        self.lock_path = lock_path or os.path.join(_config.home_dir(config), "dispatch.lock")
        self._lock_file = None
        self._dests: dict[str, Destination] = {}
        self._pool_size = self._parallel()
        self._pool = ThreadPoolExecutor(max_workers=self._pool_size, thread_name_prefix="hookspool-worker")
        self._retired_pools: list[ThreadPoolExecutor] = []
        self._alert_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hookspool-alert")
        self._work: dict[Future, _Work] = {}
        self._inflight: Counter = Counter()
        self._live: set[int] = set()
        self._last_tick = 0.0
        self._last_hb: float | None = None
        self._last_maint: float | None = None
        self._last_prune: float | None = None
        self._started_at: float | None = None
        self._alert_futs: dict[int, Future] = {}
        self._alert_wait: dict[str, float] = {}
        self._alert_unsent: set[str] = set()
        self.alerts_sent: list[tuple[str, str]] = []   # (fingerprint, text), also in dry mode
        self._seq = itertools.count(1)
        self._host = socket.gethostname()
        self.processed = 0
        self._stop: threading.Event | None = None

    # ---- parameters --------------------------------------------------------------------------
    def _dp(self) -> dict:
        return _config.merge(_config.DEFAULTS["dispatch"], self.config.get("dispatch") or {})

    def _parallel(self) -> int:
        return max(1, int(self._dp()["parallel"]))

    def _backoff(self, n: int) -> float:
        p = self._dp()
        return backoff_s(n, float(p["backoff_base_s"]), float(p["backoff_cap_s"]), self._jitter())

    # ---- clock -------------------------------------------------------------------------------
    def now_eff(self) -> float:
        """``max(wall clock, last tick)``: never moves backwards."""
        t = max(float(self.wall()), self._last_tick)
        self._last_tick = t
        return t

    def _clock(self) -> float:
        return max(float(self.wall()), self._last_tick)

    # ---- lifecycle ---------------------------------------------------------------------------
    def start(self, *, take_lock: bool = True) -> list[int]:
        """Lock, migrate, check, recover every lease (all belong to a dead process: the
        flock proves it). Raises LockHeld (exit 3) or SchemaError (exit 2)."""
        if take_lock and self._lock_file is None:
            self._lock_file = acquire_lock(self.lock_path)
        try:
            self.store.ensure_schema()
            if not self.store.quick_check():
                raise SchemaError(f"{self.store.path}: PRAGMA quick_check failed; refusing to start")
            last = self.store.meta_get("last_tick_at")
            if last:
                self._last_tick = max(self._last_tick, float(last))
            now = self.now_eff()
            self._started_at = now
            recovered = self.store.recover_leases(now, all_leases=True)
            for bid in recovered:
                log.warning("lease recovered at startup batch=%d", bid)
            self._heartbeat(now, force=True)
        except BaseException:
            self._release_lock()
            raise
        log.info("dispatcher started pid=%d parallel=%d recovered=%d", os.getpid(), self._parallel(), len(recovered))
        return recovered

    def _release_lock(self) -> None:
        if self._lock_file is not None:
            try:
                self._lock_file.close()
            finally:
                self._lock_file = None

    def close(self) -> None:
        for old in self._retired_pools:
            old.shutdown(wait=True)
        self._retired_pools.clear()
        self._pool.shutdown(wait=True)
        self._reap()
        self._alert_pool.shutdown(wait=True)
        self._release_lock()

    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        self._stop = stop
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: stop.set())
            signal.signal(signal.SIGHUP, lambda *_: self._force_reload())
        poll = float(self._dp()["poll_s"])
        while not stop.is_set():
            try:
                self._reap()
                self.tick()
            except Exception as e:   # a locked or briefly unreadable spool must not kill the loop
                log.error("tick failed: %s", e.__class__.__name__)
            if self._work:
                wait(list(self._work), timeout=poll, return_when=FIRST_COMPLETED)
            else:
                stop.wait(poll)
            poll = float(self._dp()["poll_s"])
        log.info("dispatcher stopping; waiting for %d in-flight", len(self._work))
        self.close()

    def drain_once(self, max_ticks: int = 10000) -> int:
        """Deliver everything due now (follow-on batches included), then return how many
        outcomes were recorded. For tests and ``hookspool run`` smoke checks."""
        start = self.processed
        for _ in range(max_ticks):
            self._reap()
            submitted = self.tick()
            if not self._work and submitted == 0:
                break
            if self._work:
                wait(list(self._work), return_when=FIRST_COMPLETED)
        self._reap()
        return self.processed - start

    def flush_alerts(self, timeout: float = 30.0) -> None:
        """Wait for queued alert commands (tests)."""
        futs = list(self._alert_futs.values())
        if futs:
            wait(futs, timeout=timeout)
        self._reap_alerts()

    # ---- config ------------------------------------------------------------------------------
    def _stat(self) -> float | None:
        if not self._config_path:
            return None
        try:
            return os.stat(self._config_path).st_mtime_ns
        except OSError:
            return None

    def _force_reload(self) -> None:
        self._mtime = -1

    def _maybe_reload(self, now: float) -> bool:
        if not self._config_path:
            return False
        m = self._stat()
        if m == self._mtime:
            return False
        had = self._mtime is not None
        self._mtime = m
        if m is None:
            if had:
                self._config_failed(now, f"{self._config_path} is missing; keeping the previous config")
            return False
        try:
            new = _config.load_config(self._config_path, cli_home=self._cli_home)
            rep = _config.check(new, load_plugin=_config.default_plugin_loader, check_secrets=False)
            if not rep.ok:
                raise _config.ConfigError("; ".join(rep.errors[:5]))
        except (ValueError, OSError) as e:
            self._config_failed(now, f"config reload failed, keeping the previous config: {e}")
            return False
        except Exception as e:      # noqa: BLE001 - a checker bug must not leave the reload half done
            self._config_failed(now, f"config reload failed, keeping the previous config: {e.__class__.__name__}")
            return False
        self.config = new
        self.store.config = new
        self._dests.clear()
        self._resize_pool()
        self.store.issue_resolve("config:file", now=now)
        n = self.store.redue_config_retries(now)
        log.info("config reloaded redue=%d", n)
        return True

    def _resize_pool(self) -> None:
        """A reload that raises ``parallel`` gets a bigger pool, so every claimed batch has
        a worker at once (a leased batch must never wait in the executor's queue). The old
        pool finishes what it runs; ``close`` waits for it."""
        want = self._parallel()
        if want <= self._pool_size:
            return
        self._retired_pools.append(self._pool)
        self._retired_pools[-1].shutdown(wait=False)
        self._pool = ThreadPoolExecutor(max_workers=want, thread_name_prefix="hookspool-worker")
        self._pool_size = want
        log.info("worker pool resized parallel=%d", want)

    def _config_failed(self, now: float, text: str) -> None:
        log.error("config reload failed; keeping the previous config (the reason is in `hookspool issues`)")
        self.store.issue_open("config:file", "config", error=text, now=now)

    def destination(self, name: str) -> Destination:
        d = self._dests.get(name)
        if d is None:
            d = self._factory(self.config, name)
            self._dests[name] = d
        return d

    # ---- one poll ------------------------------------------------------------------------------
    def tick(self) -> int:
        """One poll. Returns how many batches were handed to workers."""
        self._reap()
        now = self.now_eff()
        self._maybe_reload(now)
        self._heartbeat(now)
        for bid in self.store.recover_leases(now, self._live):
            log.warning("lease expired batch=%d: redelivering", bid)
        sealed = self.store.seal_ready(now)
        if sealed:
            log.debug("sealed %d batch(es)", len(sealed))
        if self._last_maint is None or now - self._last_maint >= MAINTENANCE_S:
            self._last_maint = now
            self._maintenance(now)
        self._alerts(now)
        return self._schedule(now)

    def _heartbeat(self, now: float, force: bool = False) -> None:
        hb = float(self._dp()["heartbeat_s"])
        if force or self._last_hb is None or now - self._last_hb >= hb:
            self.store.meta_set({"dispatcher_heartbeat_at": repr(now), "dispatcher_pid": os.getpid(),
                                 "last_tick_at": repr(now)})
            self._last_hb = now

    def _schedule(self, now: float) -> int:
        parallel = self._parallel()
        if len(self._work) >= parallel:
            return 0
        cands = self.store.candidates(now)
        by_dest: dict[str, list[dict]] = {}
        for b in cands:
            by_dest.setdefault(b["destination"], []).append(b)
        params = _breaker.Params.from_config(self.config)
        rows: dict[str, dict] = {}
        decided: dict[str, str] = {}
        submitted = 0
        for b in cands:
            if len(self._work) >= parallel:
                break
            name = b["destination"]
            if name == QUARANTINE or b["id"] in self._live:
                continue
            if name not in decided:
                row = self.store.destination_get(name)
                decision, upd = _breaker.gate(row, now, params)
                if upd:
                    row = self.store.destination_set(name, **upd)
                    log.info("breaker half_open destination=%s", name)
                rows[name], decided[name] = row, decision
            decision = decided[name]
            if decision == _breaker.SKIP:
                continue
            dest = self.destination(name)
            if decision == _breaker.PROBE:
                row = rows[name]
                last = row.get("last_probe_batch_id")
                pick = next((c for c in by_dest[name] if c["id"] != last and c["id"] not in self._live), b)
                decided[name] = _breaker.SKIP     # one probe per destination at a time
                if self._submit(pick, dest, now, probe=True):
                    submitted += 1
                continue
            if self._inflight[name] >= dest.max_inflight:
                continue
            if self._submit(b, dest, now, probe=False):
                submitted += 1
        return submitted

    def _submit(self, b: dict, dest: Destination, now: float, *, probe: bool) -> bool:
        bid = int(b["id"])
        owner = f"{self._host}:{os.getpid()}:{next(self._seq)}"
        lease_s = float(dest.timeout_s) + float(self._dp()["lease_grace_s"])
        if not self.store.claim(bid, lease_s, owner, now=now, probe=probe):
            return False
        lease_until = now + lease_s
        try:
            att = self.store.start_attempt(bid, owner, per_attempt=dest.idempotency == "per-attempt",
                                           probe=probe, now=now)
        except LeaseLost:
            return False
        try:
            doc = self.store.document(bid, att.delivery_id, att.n)
            body = encode_body(doc)
            payloads = None
            if getattr(dest, "unwrap", False):      # one request per event: the stored bytes, in order
                rows = self.store.stored_payloads(bid)
                if [i for i, _ in rows] != [m["id"] for m in doc["event_meta"]]:
                    raise RuntimeError("stored payloads do not match the document")
                payloads = tuple(p for _, p in rows)
        except Exception as e:      # unreadable batch: put it back, uncounted
            log.error("cannot build document batch=%d: %s", bid, e.__class__.__name__)
            try:
                self.store.mark_retry(bid, owner, f"internal error building document: {e.__class__.__name__}",
                                      "transport", now + self._backoff(1), False, attempt_id=att.id, now=now)
            except (LeaseLost, SpoolError):
                pass
            return False
        ctx = DeliveryContext(batch_id=bid, attempt=att.n, attempt_id=att.id, delivery_id=att.delivery_id,
                              probe=probe, lease_until=lease_until, deadline=lease_until - DEADLINE_MARGIN_S,
                              now=self._clock, payloads=payloads)
        w = _Work(b, dest, owner, att, probe, now, lease_until, doc, body, ctx)
        fut = self._pool.submit(self._run, w)
        self._work[fut] = w
        self._inflight[dest.name] += 1
        self._live.add(bid)
        log.info("claimed batch=%d destination=%s attempt=%d probe=%d events=%d bytes=%d", bid, dest.name, att.n,
                 int(probe), doc["count"], len(body))
        return True

    # ---- worker ------------------------------------------------------------------------------------
    @staticmethod
    def _run(w: _Work) -> Outcome:
        t0 = time.monotonic()
        try:
            cap = w.dest.max_body_bytes
            # unwrap sends each stored payload on its own: the receiver's cap applies to each one
            sizes = ([(m.get("id"), len(p)) for m, p in zip(w.doc.get("event_meta") or [], w.ctx.payloads) if p]
                     if w.ctx.payloads is not None else [])
            big = max(sizes, key=lambda x: x[1]) if sizes else None
            if w.doc.get("count", 0) == 0:
                out = Outcome.ok("empty: every event is excluded")
            elif cap is not None and w.ctx.payloads is None and len(w.body) > cap:
                out = Outcome.permanent(f"document is {len(w.body)} bytes, over the destination's"
                                        f" max_body_bytes {cap}; split the batch")
            elif cap is not None and big is not None and big[1] > cap:
                out = Outcome.permanent(f"event {big[0]} is {big[1]} bytes, over the destination's"
                                        f" max_body_bytes {cap}; exclude it")
            else:
                out = w.dest.deliver(w.doc, w.body, w.attempt.delivery_id, w.ctx)
                if not isinstance(out, Outcome):
                    out = Outcome.transport(f"internal error: deliver returned {type(out).__name__}")
        except Exception as e:     # a destination bug: transport, so the breaker and probes contain it
            out = Outcome.transport(f"internal error {e.__class__.__name__}")
        w.duration_ms = (time.monotonic() - t0) * 1000.0
        return out

    def _reap(self) -> None:
        for fut in [f for f in self._work if f.done()]:
            w = self._work.pop(fut)
            self._inflight[w.dest.name] -= 1
            try:
                out = fut.result()
                self._finish(w, out)
            except Exception as e:
                log.error("recording outcome failed batch=%d: %s", w.batch["id"], e.__class__.__name__)
            finally:
                self._live.discard(int(w.batch["id"]))
                self.processed += 1

    # ---- outcomes ----------------------------------------------------------------------------------
    def _finish(self, w: _Work, out: Outcome) -> None:
        now = self.now_eff()
        bid, name = int(w.batch["id"]), w.dest.name
        common = {"attempt_id": w.attempt.id, "status": out.status, "duration_ms": w.duration_ms, "now": now,
                  "events_delivered": out.events_delivered, "failed_event_id": out.failed_event_id}
        try:
            if out.kind == "ok":
                try:
                    self.store.mark_delivered(bid, w.owner, out.detail or out.reason, out.emits, **common)
                except InvalidEvent as e:
                    out = self._bad_emit(bid, str(e))
                else:
                    log.info("delivered batch=%d destination=%s attempt=%d status=%s duration_ms=%d emits=%d",
                             bid, name, w.attempt.n, out.status, w.duration_ms, len(out.emits))
                    self._on_success(w, out, now)
                    return
            if out.kind == "retry" and out.klass == "batch" and out.reason == "bad emit":
                # a script's own parse of its emits failed: the same twice-then-permanent rule
                out = self._bad_emit(bid, out.detail.removeprefix("bad emit: "))
            if out.kind == "retry":
                self._on_retry(w, out, now, common)
            else:
                self._on_dead(w, out, now, common)
        except LeaseLost:
            log.warning("lease lost batch=%d destination=%s attempt=%d: result discarded", bid, name, w.attempt.n)
        except SpoolError as e:
            log.error("cannot record outcome batch=%d: %s; the lease will expire and redeliver", bid,
                      e.__class__.__name__)

    def _bad_emit(self, bid: int, err: str) -> Outcome:
        """A script emitted an invalid event: a counted batch retry the first time, dead
        (permanent) when the previous attempt failed the same way."""
        b = self.store.batch(bid) or {}
        if str(b.get("last_error") or "").startswith("bad emit"):
            return Outcome.permanent("bad emit twice", detail=f"bad emit twice: {err}")
        return Outcome.batch("bad emit", detail=f"bad emit: {err}")

    def _open_issue(self, fp: str, kind: str, w: _Work, text: str, now: float) -> None:
        iid, opened = self.store.issue_open(fp, kind, destination=w.dest.name, route=w.batch.get("route"),
                                            sample_batch_id=int(w.batch["id"]), error=text, now=now)
        if opened:
            log.warning("issue opened id=%d fingerprint=%s", iid, fp)

    def _resolve_issues(self, name: str, now: float) -> None:
        want = {f"delivery:{name}:{r}" for r in TRANSIENT_REASONS} | {f"config:delivery:{name}"}
        for i in self.store.issue_list("open") + self.store.issue_list("acked"):
            if i["fingerprint"] in want and self.store.issue_resolve(int(i["id"]), now=now):
                log.info("issue resolved id=%d fingerprint=%s", i["id"], i["fingerprint"])

    def _on_success(self, w: _Work, out: Outcome, now: float) -> None:
        name, bid = w.dest.name, int(w.batch["id"])
        row = self.store.destination_get(name)
        if (row.get("state") or "closed") != "closed":
            log.info("breaker closed destination=%s", name)
        self.store.destination_set(name, **_breaker.on_success(row, now))
        self._resolve_issues(name, now)
        if out.notice:
            text = (f"{name} answered {out.notice!r} for batch {bid}: the receiver dropped it on purpose;"
                    f" check its event filter and coalesce settings")
            self._open_issue(f"delivery:{name}:ignored", "delivery", w, text, now)

    def _is_poison(self, w: _Work, row: dict, now: float) -> bool:
        """Poison by comparison (§4.2): this attempt failed with ``transport`` while the breaker is
        ``closed``, and it completes a run of ``POISON_STRIKES`` *strikes*: transport failures of this
        batch, each one after the destination accepted another batch since this batch's previous
        transport failure. The failure is then the batch's, not the destination's, so it does not
        count against the breaker. A failed probe is a failure like any other (the start of a
        run); a lease that merely expired is not evidence and is skipped; any other outcome ends
        the run. A whole outage scores nothing (no success in between), and a destination that
        fails at random with probability p kills a healthy batch with probability about
        p^(POISON_STRIKES + 1), not p."""
        if (row.get("state") or "closed") != "closed" or row.get("last_ok_at") is None or w.probe:
            return False
        bid = int(w.batch["id"])
        atts = self.store.attempts(bid)
        if not atts or atts[-1]["id"] != w.attempt.id:
            return False
        run: list[float] = []
        for a in reversed(atts[:-1]):
            if a["outcome"] == "retry" and a["klass"] == "transport":
                if a["error"] == "lease expired" or a["finished_at"] is None:
                    continue                    # a recovered lease, not a result
                run.append(float(a["finished_at"]))
                if len(run) >= POISON_STRIKES:
                    break
                continue
            break
        if len(run) < POISON_STRIKES:
            return False
        ends = sorted(run) + [float(now)]
        return all(self.store.destination_ok_between(w.dest.name, ends[i], ends[i + 1], exclude_batch=bid)
                   for i in range(len(ends) - 1))

    def _on_retry(self, w: _Work, out: Outcome, now: float, common: dict) -> None:
        name, bid = w.dest.name, int(w.batch["id"])
        err = out.detail or out.reason
        p = self._dp()
        dtype = getattr(w.dest, "type_name", "")
        if out.klass == "transport":
            row = self.store.destination_get(name)
            if self._is_poison(w, row, now):
                text = (f"batch {bid} failed {POISON_STRIKES + 1} times in a row on {name} ({out.reason}) while"
                        f" {name} accepted other batches in between: it is poison and is dead; inspect it,"
                        f" then `hookspool split`/`exclude` and `hookspool replay {bid}`")
                self.store.mark_dead(bid, w.owner, text, "poison", **common)
                if w.probe:
                    self.store.destination_set(name, probe_until=None)
                log.error("dead batch=%d destination=%s class=poison reason=%s", bid, name, out.reason)
                self._open_issue(f"delivery:{name}:poison", "delivery", w, text, now)
                return
            fields, what = _breaker.on_transport_failure(row, w.started_at, now,
                                                         _breaker.Params.from_config(self.config), scrub(out.reason))
            if w.probe:
                fields.setdefault("probe_until", None)
            n = int(fields.get("consecutive_failures", row.get("consecutive_failures") or 0) or 1)
            delay = self._backoff(n)
            self.store.mark_retry(bid, w.owner, err, "transport", now + delay, False, **common)
            self.store.destination_set(name, **fields)
            log.warning("retry batch=%d destination=%s class=transport reason=%s next_in=%.0fs failures=%d",
                        bid, name, out.reason, delay, n)
            if what:
                cooldown = fields.get("cooldown_s")
                log.error("breaker %s destination=%s cooldown_s=%s", what, name, cooldown)
                text = (f"{name} is unreachable ({out.reason}); breaker open, next probe in {cooldown:.0f}s;"
                        f" events keep spooling.")
                if what == "retripped":
                    text += (f" Last probe batch {bid} failed; if every probe fails on the same batch it may be"
                             f" a poison payload: `hookspool show {bid}`, then `exclude`/`split`.")
                self._open_issue(f"delivery:{name}:{reason_class(out, dtype)}", "delivery", w, text, now)
            return
        if w.probe:
            self.store.destination_set(name, probe_until=None)
        if out.klass == "throttle":
            row = self.store.destination_get(name)
            delay = self._backoff(max(1, int(row.get("consecutive_failures") or 0)))
            ra = None
            if out.retry_after_s is not None and out.retry_after_s > 0:
                ra = min(float(out.retry_after_s), RETRY_AFTER_CAP_S)
                delay = max(delay, ra)
            self.store.mark_retry(bid, w.owner, err, "throttle", now + delay, False, **common)
            if ra is not None:
                self.store.destination_set(name, retry_after_until=now + ra)
            log.warning("retry batch=%d destination=%s class=throttle next_in=%.0fs", bid, name, delay)
            return
        if out.klass == "config":
            delay = float(p["backoff_cap_s"])
            self.store.mark_retry(bid, w.owner, err, "config", now + delay, False, **common)
            log.warning("retry batch=%d destination=%s class=config reason=%s next_in=%.0fs", bid, name,
                        out.reason, delay)
            self._open_issue(f"config:delivery:{name}", "config", w,
                             f"{name}: {out.reason}; batch {bid} waits at the head of its group and is retried"
                             f" at once after a successful config reload", now)
            return
        # batch: counted
        b = self.store.batch(bid) or {}
        n = int(b.get("attempts") or 0) + 1
        rc = reason_class(out, dtype)
        if n >= int(p["max_attempts"]):
            text = (f"batch {bid} on {name} failed {n} counted attempts ({out.reason}) and is dead; fix, then:"
                    f" `hookspool replay {bid}`")
            self.store.mark_dead(bid, w.owner, err, "batch", counted=True, **common)
            log.error("dead batch=%d destination=%s class=batch attempts=%d reason=%s", bid, name, n, out.reason)
            self._open_issue(f"delivery:{name}:exhausted", "delivery", w, text, now)
            return
        delay = self._backoff(n)
        self.store.mark_retry(bid, w.owner, err, "batch", now + delay, True, **common)
        log.warning("retry batch=%d destination=%s class=batch reason=%s attempts=%d next_in=%.0fs", bid, name,
                    out.reason, n, delay)
        if n >= STREAK_ISSUE_AT:
            self._open_issue(f"delivery:{name}:{rc}", "delivery", w,
                             f"{name} keeps failing batch {bid} ({out.reason}); attempt {n} of"
                             f" {int(p['max_attempts'])}", now)

    def _on_dead(self, w: _Work, out: Outcome, now: float, common: dict) -> None:
        name, bid = w.dest.name, int(w.batch["id"])
        self.store.mark_dead(bid, w.owner, out.detail or out.reason, "permanent", **common)
        if w.probe:
            self.store.destination_set(name, probe_until=None)
        log.error("dead batch=%d destination=%s class=permanent status=%s reason=%s", bid, name, out.status,
                  out.reason)
        self._open_issue(f"delivery:{name}:permanent", "delivery", w,
                         f"{name} rejected batch {bid} ({out.reason}); it is dead. Fix or `hookspool split`/"
                         f"`exclude` it, then `hookspool replay {bid}`", now)

    # ---- maintenance ------------------------------------------------------------------------------
    def _maintenance(self, now: float) -> None:
        if self._last_prune is None or now - self._last_prune >= RETENTION_S:
            self._last_prune = now
            out = self.store.prune(now)
            if any(out.values()):
                log.info("retention " + " ".join(f"{k}=%d" for k in out), *out.values())
        self._check_sources(now)
        self._resolve_exhausted(now)

    def _check_sources(self, now: float) -> None:
        for name, s in (self.config.get("sources") or {}).items():
            exp = (s or {}).get("expect_every_s") if isinstance(s, dict) else None
            if not exp:
                continue
            fp = f"source_silent:{name}"
            v = self.store.meta_get(f"source:{name}:last_event_at")
            last = float(v) if v is not None else None
            base = last
            if base is None:
                # a source that never sent: measure from when it was first watched, persisted,
                # so a dispatcher restart does not restart the clock
                key = f"source:{name}:watched_since"
                w = self.store.meta_get(key)
                if w is None:
                    self.store.meta_set({key: repr(now)})
                    w = repr(now)
                base = float(w)
            silent = now - base > float(exp)
            cur = self.store.issue_get(fp)
            active = cur is not None and cur["state"] in ("open", "acked", "ignored")
            if silent and not active:
                ago = "never" if last is None else f"{now - last:.0f}s ago"
                iid, _ = self.store.issue_open(fp, "source_silent", route=name, now=now,
                                               error=f"source {name} sent nothing for longer than {exp}s"
                                                     f" (last event: {ago}); its sweep covers the gap")
                log.warning("issue opened id=%d fingerprint=%s", iid, fp)
            elif not silent and cur is not None and cur["state"] in ("open", "acked"):
                self.store.issue_resolve(fp, now=now)
                log.info("issue resolved fingerprint=%s", fp)

    def _resolve_exhausted(self, now: float) -> None:
        for i in self.store.issue_list("open") + self.store.issue_list("acked"):
            fp = i["fingerprint"]
            if not (fp.startswith("delivery:") and fp.endswith(":exhausted")) or not i["destination"]:
                continue
            left = [b for b in self.store.list("dead", destination=i["destination"], limit=100000)
                    if b["last_class"] == "batch"]
            if not left:
                self.store.issue_resolve(int(i["id"]), now=now)
                log.info("issue resolved id=%d fingerprint=%s", i["id"], fp)

    # ---- alerts (§11.4) ---------------------------------------------------------------------------
    def _reap_alerts(self) -> None:
        for iid in [i for i, f in self._alert_futs.items() if f.done()]:
            f = self._alert_futs.pop(iid)
            if f.exception() is not None:
                log.error("alert worker failed issue=%d: %s", iid, f.exception().__class__.__name__)

    def _alerts(self, now: float) -> None:
        self._reap_alerts()
        for issue in self.store.issues_to_alert():
            iid, fp = int(issue["id"]), issue["fingerprint"]
            if iid in self._alert_futs or self._alert_wait.get(fp, 0.0) > now:
                continue
            self._alert_futs[iid] = self._alert_pool.submit(self._send_alert, issue, now)

    @staticmethod
    def alert_text(issue: dict, suppressed: int = 0) -> str:
        t = f"hookspool {issue['kind']} issue #{issue['id']} {issue['fingerprint']}"
        if issue.get("last_error"):
            t += f": {issue['last_error']}"
        if issue.get("count", 1) > 1:
            t += f" (seen {issue['count']} times)"
        if suppressed:
            t += f" (+{suppressed} suppressed since the last alert of this kind)"
        return t

    def _send_alert(self, issue: dict, now: float) -> bool:
        """Runs on the alert thread, never the main loop. ``alerted_at`` is set only when
        the command exits 0 (or in dry mode)."""
        a = _config.merge(_config.DEFAULTS["alerts"], self.config.get("alerts") or {})
        fp, iid = issue["fingerprint"], int(issue["id"])
        rate = float(a["rate_limit_s"])
        suppressed = 0
        if fp not in self._alert_unsent:
            send, suppressed = self.store.alert_gate(issue["kind"], rate, now)   # per kind (§11.4)
            if not send:
                self._alert_wait[fp] = max(now + 1.0, self.store.alert_open_at(issue["kind"], rate))
                log.info("alert rate-limited issue=%d fingerprint=%s", iid, fp)
                return False
        text = self.alert_text(issue, suppressed)
        if _config.alerts_dry(self.config):
            self.alerts_sent.append((fp, text))
            log.warning("alert (dry) issue=%d fingerprint=%s", iid, fp)
            ok = True
        elif not a.get("argv"):
            ok = False
            log.error("alert not sent issue=%d fingerprint=%s: alerts.argv is empty", iid, fp)
        else:
            argv = [str(x) for x in a["argv"]] + [text]
            try:
                r = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=float(a["timeout_s"]))
                ok = r.returncode == 0
                log.warning("alert sent issue=%d fingerprint=%s exit=%d", iid, fp, r.returncode)
            except (OSError, subprocess.TimeoutExpired, IndexError) as e:
                ok = False
                log.error("alert failed issue=%d fingerprint=%s: %s", iid, fp, e.__class__.__name__)
            if ok:
                self.alerts_sent.append((fp, text))
        if ok:
            self.store.issue_mark_alerted(iid, now=now)
            self._alert_unsent.discard(fp)
        else:
            self._alert_unsent.add(fp)        # nothing went out: the retry skips the rate limit
            self._alert_wait[fp] = now + ALERT_RETRY_S
        return ok


# ---- CLI entry (`hookspool dispatch` / `hookspool run`) --------------------------------------------
def setup_logging(component: str = "dispatch") -> None:
    """The shared service log format (``hookspool.logs``): one line per record, the fixed fields
    only, control characters escaped in text mode."""
    root = logging.getLogger("hookspool")
    if any(getattr(h, "_hookspool", False) for h in root.handlers):
        return
    _logs.setup()


def cli(a, cfg: dict) -> int:
    """``hookspool dispatch`` (and ``run``: the intake in a thread plus the dispatcher)."""
    setup_logging("run" if getattr(a, "cmd", "") == "run" else "dispatch")
    home = _config.home_dir(cfg, getattr(a, "home", None))
    db = _config.db_path(cfg, getattr(a, "db", None), getattr(a, "home", None))
    cfg_path = _config.config_path(getattr(a, "config", None), getattr(a, "home", None))
    store = Store(db, cfg, busy_timeout_s=30)
    d = Dispatcher(store, cfg, config_path=cfg_path, lock_path=os.path.join(home, "dispatch.lock"),
                   cli_home=getattr(a, "home", None))
    try:
        d.start()
    except LockHeld as e:
        print(f"hookspool: {e}: another dispatcher is running", file=sys.stderr)
        d._pool.shutdown(wait=False)
        d._alert_pool.shutdown(wait=False)
        return EXIT_LOCK
    except (SchemaError, SpoolError) as e:
        print(f"hookspool: {e}", file=sys.stderr)
        d._pool.shutdown(wait=False)
        d._alert_pool.shutdown(wait=False)
        return EXIT_CONFIG
    if getattr(a, "cmd", "") == "run":
        try:
            from . import intake as _intake
        except ImportError:
            print("hookspool: `run` needs the intake, which is not in this build", file=sys.stderr)
            d.close()
            return EXIT_CONFIG
        import argparse
        ia = argparse.Namespace(**dict(vars(a), cmd="intake"))
        threading.Thread(target=_intake.cli, args=(ia, cfg), name="hookspool-intake", daemon=True).start()
    d.run_forever()
    return EXIT_OK
