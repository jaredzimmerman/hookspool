"""Destination registry (C3, §14.2): built-in types resolve, overrides apply, bad tables degrade
to ``Unavailable`` (a config retry), and the three types declare the idempotency the contract fixes."""
import pytest

from hookspool.destinations import build
from hookspool.destinations.base import Unavailable
from hookspool.destinations.hermes import HermesDestination
from hookspool.destinations.http import HttpDestination
from hookspool.destinations.script import ScriptDestination


def test_builtin_types_and_fixed_idempotency(make_config):
    cfg = make_config(destinations={"script:s": {"argv": ["true"]},
                                    "audit": {"type": "http", "url": "https://example.invalid/h",
                                              "secret": {"env": "X"}}})
    h, s, a = build(cfg, "hermes:test-route"), build(cfg, "script:s"), build(cfg, "audit")
    assert (type(h), type(s), type(a)) == (HermesDestination, ScriptDestination, HttpDestination)
    assert (h.idempotency, s.idempotency, a.idempotency) == ("per-attempt", "per-batch", "per-batch")
    assert (h.max_inflight, s.max_inflight, a.max_inflight) == (2, 1, 4)


def test_idempotency_cannot_be_configured(make_config):
    cfg = make_config(destinations={"hermes:test-route": {"idempotency": "per-batch"}})
    assert build(cfg, "hermes:test-route").idempotency == "per-attempt"


def test_every_destination_declares_max_body_bytes(make_config):
    cfg = make_config(destinations={"script:s": {"argv": ["true"]},
                                    "audit": {"type": "http", "url": "https://example.invalid/h",
                                              "secret": {"env": "X"}, "max_body_bytes": 4096}})
    assert build(cfg, "hermes:test-route").max_body_bytes == 1048576
    assert build(cfg, "audit").max_body_bytes == 4096
    assert build(cfg, "script:s").max_body_bytes is None          # unbounded unless configured


@pytest.mark.parametrize("name", ["nonsense", "quarantine", "ftp:x"])
def test_unbuildable_names_are_unavailable_and_retry_as_config(make_config, name):
    from _httpstub import doc_for, encode, make_ctx
    d = build(make_config(), name)
    assert isinstance(d, Unavailable)
    doc = doc_for()
    o = d.deliver(doc, encode(doc), "id", make_ctx())
    assert (o.kind, o.klass) == ("retry", "config") and d.check()
