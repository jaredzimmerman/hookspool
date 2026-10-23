"""Per-destination circuit breaker and operator pause (ARCHITECTURE §4.2, §6.3).

Pure functions over a ``destinations`` row (a dict). They return the fields to write;
the dispatcher writes them from its main loop thread, so breaker updates are serialised.

- ``trip_after`` consecutive transport failures on the destination (any batches) ->
  ``open``, cooldown ``cooldown_initial_s``.
- Only an attempt that started after ``opened_at`` (a probe) re-trips and doubles the
  cooldown (capped at ``cooldown_max_s``); attempts already in flight when the breaker
  opened record their outcome and nothing else.
- ``open`` -> ``half_open`` when the cooldown elapses; one probe at a time, none while
  ``probe_until > now``.
- Any success closes it and resets the counters. ``paused`` is the operator's and only
  ``hookspool resume`` leaves it.
- ``retry_after_until`` is a soft gate: no claims before it, no state change.

Nothing here discards a batch.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config as _config


@dataclass(frozen=True)
class Params:
    trip_after: int = 3
    cooldown_initial_s: float = 30.0
    cooldown_max_s: float = 600.0

    @classmethod
    def from_config(cls, cfg: dict) -> "Params":
        b = _config.merge(_config.DEFAULTS["dispatch"]["breaker"], ((cfg.get("dispatch") or {}).get("breaker") or {}))
        return cls(max(1, int(b["trip_after"])), float(b["cooldown_initial_s"]), float(b["cooldown_max_s"]))


SKIP, CLAIM, PROBE = "skip", "claim", "probe"


def gate(d: dict, now: float, p: Params) -> tuple[str, dict]:
    """(decision, fields to write) for claiming batches of this destination now."""
    state = d.get("state") or "closed"
    if state == "paused":
        return SKIP, {}
    upd: dict = {}
    if state == "open":
        cooldown = d.get("cooldown_s") or p.cooldown_initial_s
        if now < (d.get("opened_at") or 0.0) + cooldown:
            return SKIP, {}
        state = "half_open"
        upd = {"state": "half_open", "probe_until": None}
    ra = d.get("retry_after_until")
    if ra is not None and ra > now:
        return SKIP, upd
    if state == "half_open":
        pu = None if upd else d.get("probe_until")
        if pu is not None and pu > now:
            return SKIP, upd
        return PROBE, upd
    return CLAIM, upd


def on_success(d: dict, now: float) -> dict:
    fields = {"consecutive_failures": 0, "opened_at": None, "cooldown_s": None, "probe_until": None,
              "last_ok_at": now, "last_error": None}
    if (d.get("state") or "closed") != "paused":
        fields["state"] = "closed"
    return fields


def on_transport_failure(d: dict, started_at: float, now: float, p: Params, error: str) -> tuple[dict, str | None]:
    """Fields to write after a transport failure, and what happened: ``"tripped"``
    (closed -> open), ``"retripped"`` (a probe failed), or None."""
    state = d.get("state") or "closed"
    fails = int(d.get("consecutive_failures") or 0)
    if state == "paused":
        return {"last_error": error}, None
    if state == "closed":
        fails += 1
        fields: dict = {"consecutive_failures": fails, "last_error": error}
        if fails >= p.trip_after:
            fields.update(state="open", opened_at=now, cooldown_s=p.cooldown_initial_s, probe_until=None)
            return fields, "tripped"
        return fields, None
    # open or half_open: only an attempt that started after the trip may re-trip
    opened = d.get("opened_at")
    if opened is not None and started_at <= opened:
        return {"last_error": error}, None
    cooldown = min(p.cooldown_max_s, (d.get("cooldown_s") or p.cooldown_initial_s) * 2)
    return {"consecutive_failures": fails + 1, "last_error": error, "state": "open", "opened_at": now,
            "cooldown_s": cooldown, "probe_until": None}, "retripped"
