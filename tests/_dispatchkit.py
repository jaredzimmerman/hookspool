"""Shared rig for the dispatcher tests: a migrated store on the fake clock, a Dispatcher
with zero jitter, and scripted stub destinations (the C3 base API, no network)."""
from __future__ import annotations

import threading

import pytest

from conftest import SENTINEL
from hookspool import config as hs_config
from hookspool.destinations.base import Destination, Outcome
from hookspool.dispatch import Dispatcher

DEST = "hermes:test-route"


class StubDestination(Destination):
    """Pops one scripted outcome per delivery (an Outcome, or ``fn(doc, ctx) -> Outcome``);
    ok when the script is empty. ``hold`` (an Event) blocks deliveries until set."""

    type_name = "stub"

    def __init__(self, name, config, *, idempotency="per-batch", timeout_s=30.0, max_inflight=2,
                 max_body_bytes=None):
        spec = {"timeout_s": timeout_s, "max_inflight": max_inflight}
        if max_body_bytes is not None:
            spec["max_body_bytes"] = max_body_bytes
        super().__init__(name, spec, config)
        self.idempotency = idempotency
        self.outcomes: list = []
        self.calls: list[dict] = []
        self.hold: threading.Event | None = None
        self.started = threading.Semaphore(0)
        self._lock = threading.Lock()

    def script(self, *outcomes) -> "StubDestination":
        self.outcomes.extend(outcomes)
        return self

    def deliver(self, doc, body, delivery_id, ctx):
        with self._lock:
            self.calls.append({"doc": doc, "body": body, "delivery_id": delivery_id, "ctx": ctx})
            o = self.outcomes.pop(0) if self.outcomes else Outcome.ok()
        self.started.release()
        if self.hold is not None:
            self.hold.wait(20)
        return o(doc, ctx) if callable(o) else o


def payload(n: int = 0, **kw) -> dict:
    return dict(kw, n=n, note=SENTINEL)


class Rig:
    def __init__(self, store, cfg, clock, tmp_path, **dkw):
        self.store, self.cfg, self.clock, self.tmp_path = store, cfg, clock, tmp_path
        self.stubs: dict[str, StubDestination] = {}
        self.dkw = dkw
        self.d = self.dispatcher(**dkw)

    def factory(self, cfg, name):
        if name not in self.stubs:
            self.stubs[name] = StubDestination(name, cfg)
        return self.stubs[name]

    def dispatcher(self, **kw) -> Dispatcher:
        kw.setdefault("now", self.clock)
        kw.setdefault("destination_factory", self.factory)
        kw.setdefault("jitter", lambda: 0.0)
        kw.setdefault("lock_path", str(self.tmp_path / "home" / "dispatch.lock"))
        return Dispatcher(self.store, self.cfg, **kw)

    def stub(self, name: str = DEST, **kw) -> StubDestination:
        s = StubDestination(name, self.cfg, **kw)
        self.stubs[name] = s
        self.d._dests.pop(name, None)
        return s

    def enqueue(self, key: str = "k", dest: str = DEST, n: int = 0, **kw):
        kw.setdefault("quiet_s", 0)
        kw.setdefault("max_s", 0)
        return self.store.enqueue("src", key, dest, payload(n), **kw)

    def batch(self, bid: int) -> dict:
        return self.store.batch(bid)

    def dest(self, name: str = DEST) -> dict:
        return self.store.destination_get(name)

    def at(self, t: float) -> None:
        self.clock.t = t


@pytest.fixture
def rig(make_store, make_config, clock, tmp_path):
    made = []

    def make(cfg_over: dict | None = None, *, start: bool = True, **dkw) -> Rig:
        cfg = make_config(**(cfg_over or {}))
        st = make_store(cfg)
        r = Rig(st, cfg, clock, tmp_path, **dkw)
        if start:
            r.d.start()
        made.append(r)
        return r
    yield make
    for r in made:
        for s in r.stubs.values():
            if s.hold is not None:
                s.hold.set()
        r.d.close()


def config_dict(**over) -> dict:
    return hs_config.merge(hs_config.DEFAULTS, over)
