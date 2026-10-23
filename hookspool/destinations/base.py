"""Destination base API (contract C3; ARCHITECTURE §6.4, §6.5, §14.2).

A destination type is a ``Destination`` subclass. The dispatcher builds one instance per
destination name, claims a batch, writes the attempt row, then calls::

    outcome = dest.deliver(doc, body, delivery_id, ctx)

from a worker thread. ``doc`` is the §6.4 document, ``body`` its exact bytes (sign and
send these), ``delivery_id`` the ``webhook-id`` (per attempt or per batch, fixed by the
type's ``idempotency``), ``ctx`` the attempt's context. ``deliver`` must return before
``ctx.deadline`` (``lease_until - 5 s``), which every destination MUST honour: a worker thread
cannot be aborted, so a ``deliver`` that outlives it holds its worker, its lease and its
group's head until it returns. Use ``ctx.timeout(...)`` for socket timeouts
and kill a subprocess's process group when it runs out. It returns an ``Outcome`` and
never raises on purpose; an exception is recorded as a transport failure.

``Outcome.reason`` is short and safe for logs and alerts; ``detail`` goes to
``last_error`` (scrubbed to 500 chars) and is never logged; ``notice`` (Hermes
``ignored``/``coalesced``) opens an issue without changing the outcome.

A destination with ``unwrap = True`` (idempotency ``per-event``) delivers each event's stored
payload as its own request instead of the document. The dispatcher then fills
``ctx.payloads`` (the stored payload bytes, aligned with ``doc["event_meta"]``) and the
outcome reports ``events_delivered`` and ``failed_event_id`` for the attempt row.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, ClassVar, Literal

from .. import config as _config

KINDS = ("ok", "retry", "dead")
RETRY_CLASSES = ("transport", "throttle", "batch", "config")
DEAD_CLASSES = ("permanent",)


@dataclass
class Outcome:
    kind: Literal["ok", "retry", "dead"]
    klass: str | None = None
    status: int | None = None          # HTTP status or exit code
    reason: str = ""                   # safe for logs and alerts
    detail: str = ""                   # stored in last_error, never logged
    retry_after_s: float | None = None  # throttle only
    emits: list = field(default_factory=list)   # script emits ({"enqueue": {...}} objects' insides)
    notice: str | None = None          # e.g. "ignored": opens an issue, outcome unchanged
    events_delivered: int | None = None  # unwrap: events accepted by this attempt, in order
    failed_event_id: int | None = None   # unwrap: the event whose request failed (the attempt stopped there)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"outcome kind must be one of {KINDS}, not {self.kind!r}")
        if self.kind == "ok" and self.klass is not None:
            raise ValueError("an ok outcome has no class")
        if self.kind == "retry" and self.klass not in RETRY_CLASSES:
            raise ValueError(f"retry class must be one of {RETRY_CLASSES}, not {self.klass!r}")
        if self.kind == "dead" and self.klass not in DEAD_CLASSES:
            raise ValueError(f"a destination's dead outcome is {DEAD_CLASSES}, not {self.klass!r}")
        if self.emits and self.kind != "ok":
            raise ValueError("only an ok outcome carries emits")

    # ---- constructors (§6.5 table) ----------------------------------------------------------
    @classmethod
    def ok(cls, reason: str = "ok", *, status: int | None = None, detail: str = "", emits: list | None = None,
           notice: str | None = None) -> "Outcome":
        return cls("ok", None, status, reason, detail, None, list(emits or []), notice)

    @classmethod
    def transport(cls, reason: str, *, status: int | None = None, detail: str = "") -> "Outcome":
        """Refused, reset, timeout, worker deadline, DNS, 5xx, cannot start, script timeout:
        uncounted, a breaker failure."""
        return cls("retry", "transport", status, reason, detail)

    @classmethod
    def throttle(cls, reason: str, *, status: int | None = None, retry_after_s: float | None = None,
                 detail: str = "") -> "Outcome":
        """429 or 503 with Retry-After: uncounted, breaker neutral."""
        return cls("retry", "throttle", status, reason, detail, retry_after_s)

    @classmethod
    def batch(cls, reason: str, *, status: int | None = None, detail: str = "") -> "Outcome":
        """The destination ran and failed on this batch (non-zero script exit): counted."""
        return cls("retry", "batch", status, reason, detail)

    @classmethod
    def config(cls, reason: str, *, status: int | None = None, detail: str = "") -> "Outcome":
        """401/403/404/405/410, unknown destination, unreadable secret or file: uncounted,
        retried at the backoff cap with a config issue, due at once after a reload."""
        return cls("retry", "config", status, reason, detail)

    @classmethod
    def permanent(cls, reason: str, *, status: int | None = None, detail: str = "") -> "Outcome":
        """400/413/415/422, 3xx on http: the document itself is rejected -> dead."""
        return cls("dead", "permanent", status, reason, detail)


@dataclass(frozen=True)
class DeliveryContext:
    batch_id: int
    attempt: int                 # attempt number n (X-Hookspool-Attempt)
    attempt_id: int
    delivery_id: str
    probe: bool
    lease_until: float
    deadline: float              # lease_until - 5 s; deliver must not outlive it
    now: Callable[[], float]     # the dispatcher's clock (never earlier than its last tick)
    payloads: tuple | None = None   # unwrap only: each event's stored payload bytes, in event_meta order

    def remaining(self) -> float:
        return self.deadline - self.now()

    def timeout(self, timeout_s: float) -> float:
        """The per-operation timeout bounded by the deadline (0 means: out of time)."""
        return max(0.0, min(float(timeout_s), self.remaining()))


def encode_body(doc: dict) -> bytes:
    """The exact bytes sent for a document (and signed)."""
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class Destination:
    """Base class. Subclasses set ``type_name``, ``idempotency`` and the defaults, and
    implement ``deliver`` (and ``check``)."""

    type_name: ClassVar[str] = ""
    idempotency: ClassVar[Literal["per-batch", "per-attempt", "per-event"]] = "per-batch"
    unwrap: bool = False         # True: one request per event, body = the stored payload (see unwrap_deliver)
    default_timeout_s: ClassVar[float] = 30.0
    default_max_body_bytes: ClassVar[int | None] = None

    def __init__(self, name: str, spec: dict | None, config: dict):
        self.name = name
        self.spec = dict(spec or {})
        self.config = config
        t = self.spec.get("timeout_s")
        self.timeout_s = float(t) if t is not None else float(self._default_timeout(config))
        mb = self.spec.get("max_body_bytes")
        if mb is None:
            mb = _config.destination_max_body_bytes(config, name)
        self.max_body_bytes: int | None = int(mb) if mb is not None else self.default_max_body_bytes
        mi = self.spec.get("max_inflight")
        if mi is None:
            mi = _config.DEFAULT_MAX_INFLIGHT.get(self.type_name, 1)
        self.max_inflight = max(1, int(mi))

    def _default_timeout(self, config: dict) -> float:
        return self.default_timeout_s

    def deliver(self, doc: dict, body: bytes, delivery_id: str, ctx: DeliveryContext) -> Outcome:
        raise NotImplementedError

    def check(self) -> list[str]:
        """Config or secret problems for ``hookspool check``; never secret values."""
        return []

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.name}>"


class Unavailable(Destination):
    """Stands in for a destination that cannot be built (unknown name or type, a module
    that does not import, a bad table). Every attempt is a ``config`` retry, so batches
    wait at the head of their group until a reload fixes it."""

    type_name = "unavailable"

    def __init__(self, name: str, config: dict, why: str):
        super().__init__(name, None, config)
        self.why = why

    def deliver(self, doc: dict, body: bytes, delivery_id: str, ctx: DeliveryContext) -> Outcome:
        return Outcome.config(self.why, detail=self.why)

    def check(self) -> list[str]:
        return [f"{self.name}: {self.why}"]

