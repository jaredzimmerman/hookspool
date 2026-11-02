"""Receipts for work still running (ARCHITECTURE §6.4.3): opt-in ``receipts``,
``awaiting_receipt`` and ``uncertain`` states, idempotent settlement, failure table
and the ordering default."""
from __future__ import annotations


from _dispatchkit import DEST
from conftest import T0
from hookspool import config as hs_config
from hookspool.destinations import build
from hookspool.destinations.base import DeliveryContext, Outcome
from hookspool.destinations.http import _parse_receipt_id
from hookspool.destinations.wire import Response


# ---- config ---------------------------------------------------------------------

def test_destination_receipts_opt_in(make_config):
    cfg = make_config(destinations={
        "out": {"type": "http", "url": "https://example.invalid/h", "secret": {"env": "X"}},
    })
    assert hs_config.destination_receipts(cfg, "out") is False
    assert hs_config.destination_receipts(cfg, "missing") is False
    cfg = make_config(destinations={
        "out": {"type": "http", "url": "https://example.invalid/h", "secret": {"env": "X"}, "receipts": True},
    })
    assert hs_config.destination_receipts(cfg, "out") is True
    # hermes-named destinations qualify too
    cfg = make_config(destinations={"hermes:test-route": {"receipts": True}})
    assert hs_config.destination_receipts(cfg, "hermes:test-route") is True
    # script destinations do not take receipts
    cfg = make_config(destinations={"s": {"type": "script", "argv": ["true"], "receipts": True}})
    assert hs_config.destination_receipts(cfg, "s") is False


def test_destination_receipt_dispositions(make_config):
    cfg = make_config(destinations={
        "out": {"type": "http", "url": "https://example.invalid/h", "secret": {"env": "X"},
                "receipts": True,
                "receipt_dispositions": {"visible": "settled", "silent": "retry", "blocked": "issue"}},
    })
    assert hs_config.destination_receipt_dispositions(cfg, "out") == {
        "visible": "settled", "silent": "retry", "blocked": "issue"}
    assert hs_config.destination_receipt_dispositions(make_config(), "out") == {}


def test_receipts_keys_pass_check(make_config, tmp_path):
    (tmp_path / "s.secret").write_text("test-secret-A\n")
    cfg = make_config(destinations={
        "out": {"type": "http", "url": "https://example.invalid/h",
                "secret": {"file": str(tmp_path / "s.secret")},
                "receipts": True, "receipt_dispositions": {"visible": "settled"}},
    })
    rep = hs_config.check(cfg)
    assert rep.ok, rep.errors


# ---- destinations parse a receipt id on admission -------------------------------

def test_http_parse_receipt_id():
    assert _parse_receipt_id(b'{"receipt_id": "rcpt-7"}') == "rcpt-7"
    assert _parse_receipt_id(b'{"receipt": "rcpt-7"}') == "rcpt-7"
    assert _parse_receipt_id(b'{"status": "ok"}') is None
    assert _parse_receipt_id(b"not json") is None
    assert _parse_receipt_id(b"") is None
    assert _parse_receipt_id(b'["rcpt-7"]') is None


def test_hermes_outcome_reads_receipt_id(make_config):
    cfg = make_config(destinations={"hermes:test-route": {"receipts": True}})
    d = build(cfg, "hermes:test-route")
    ctx = DeliveryContext(batch_id=3, attempt=1, attempt_id=9, delivery_id="hs_x_3", probe=False,
                          lease_until=T0 + 60, deadline=T0 + 60, now=lambda: T0)
    out = d._outcome(Response(status=200, body=b'{"receipt_id": "rcpt-7"}'), ctx)
    assert out.kind == "ok" and out.receipt_id == "rcpt-7"
    out = d._outcome(Response(status=200, body=b'{"receipt": "rcpt-7"}'), ctx)
    assert out.receipt_id == "rcpt-7"
    out = d._outcome(Response(status=200, body=b'{}'), ctx)
    assert out.receipt_id is None
    # without opt-in, receipt_id is dropped even when the body sends one
    cfg2 = make_config(destinations={"hermes:test-route": {}})
    d2 = build(cfg2, "hermes:test-route")
    out = d2._outcome(Response(status=200, body=b'{"receipt_id": "rcpt-7"}'), ctx)
    assert out.receipt_id is None


# ---- store ----------------------------------------------------------------------

def _setup_batch(s, cfg=None):
    bid = s.enqueue("src", "k", "out", {"n": 0}, quiet_s=0, max_s=0).batch_id
    s.seal_ready(s.now())
    assert s.claim(bid, 60, "owner") is True
    return bid


def test_mark_awaiting_receipt_transitions_and_stores_receipt(make_store, make_config, clock):
    cfg = make_config(destinations={"out": {"type": "http", "url": "https://example.invalid/h",
                "secret": {"env": "X"}, "receipts": True}})
    s = make_store(cfg)
    bid = _setup_batch(s)
    s.mark_awaiting_receipt(bid, "owner", "rcpt-1", attempt_id=5, status=200, duration_ms=10, now=clock.t)
    b = s.batch(bid)
    assert b["state"] == "awaiting_receipt"
    assert b["receipt_id"] == "rcpt-1"
    assert b["attempts"] == 1
    assert b["next_attempt_at"] is None
    r = s.receipt("rcpt-1")
    assert r is not None and r["batch_id"] == bid and r["disposition"] is None
    assert r["expiry"] == clock.t + 3600.0
    # custom expiry
    bid2 = _setup_batch(s)
    s.mark_awaiting_receipt(bid2, "owner", "rcpt-2", expiry=clock.t + 100.0, now=clock.t)
    assert s.receipt("rcpt-2")["expiry"] == clock.t + 100.0
    assert s.receipts_for_batch(bid2) and len(s.receipts_for_batch(bid2)) == 1


def test_settle_receipt_settles_and_is_idempotent(make_store, make_config, clock):
    cfg = make_config(destinations={"out": {"type": "http", "url": "https://example.invalid/h",
                "secret": {"env": "X"}, "receipts": True,
                "receipt_dispositions": {"visible": "settled"}}})
    s = make_store(cfg)
    bid = _setup_batch(s)
    s.mark_awaiting_receipt(bid, "owner", "rcpt-1", now=clock.t)
    assert s.settle_receipt("rcpt-1", "visible") == "settled"
    assert s.batch(bid)["state"] == "delivered"
    assert s.receipt("rcpt-1") is not None and s.receipt("rcpt-1")["settled_at"] is not None
    # idempotent: second call is a no-op
    assert s.settle_receipt("rcpt-1", "visible") == "duplicate"
    assert s.batch(bid)["state"] == "delivered"


def test_settle_receipt_unknown_returns_none(make_store, make_config, clock):
    cfg = make_config(destinations={"out": {"type": "http", "url": "https://example.invalid/h",
                "secret": {"env": "X"}, "receipts": True,
                "receipt_dispositions": {"visible": "settled"}}})
    s = make_store(cfg)
    assert s.settle_receipt("no-such-receipt", "visible") is None


def test_settle_receipt_retry_state(make_store, make_config, clock):
    cfg = make_config(destinations={"out": {"type": "http", "url": "https://example.invalid/h",
                "secret": {"env": "X"}, "receipts": True,
                "receipt_dispositions": {"silent": "retry"}}})
    s = make_store(cfg)
    bid = _setup_batch(s)
    s.mark_awaiting_receipt(bid, "owner", "rcpt-1", now=clock.t)
    assert s.settle_receipt("rcpt-1", "silent") == "retry"
    assert s.batch(bid)["state"] == "retry"


def test_settle_receipt_issue_uncertain_opens_issue(make_store, make_config, clock):
    cfg = make_config(destinations={"out": {"type": "http", "url": "https://example.invalid/h",
                "secret": {"env": "X"}, "receipts": True,
                "receipt_dispositions": {"blocked": "issue"}}})
    s = make_store(cfg)
    bid = _setup_batch(s)
    s.mark_awaiting_receipt(bid, "owner", "rcpt-1", now=clock.t)
    assert s.settle_receipt("rcpt-1", "blocked") == "issue"
    assert s.batch(bid)["state"] == "uncertain"
    assert "receipt:uncertain:out" in {i["fingerprint"] for i in s.issue_list()}


def test_settle_receipt_unmapped_disposition_defaults_to_issue(make_store, make_config, clock):
    cfg = make_config(destinations={"out": {"type": "http", "url": "https://example.invalid/h",
                "secret": {"env": "X"}, "receipts": True,
                "receipt_dispositions": {}}})
    s = make_store(cfg)
    bid = _setup_batch(s)
    s.mark_awaiting_receipt(bid, "owner", "rcpt-1", now=clock.t)
    assert s.settle_receipt("rcpt-1", "weird") == "issue"
    assert s.batch(bid)["state"] == "uncertain"


def test_settle_receipt_records_handled_events(make_store, make_config, clock):
    cfg = make_config(destinations={"out": {"type": "http", "url": "https://example.invalid/h",
                "secret": {"env": "X"}, "receipts": True,
                "receipt_dispositions": {"silent": "retry"}}})
    s = make_store(cfg)
    bid = _setup_batch(s)
    s.mark_awaiting_receipt(bid, "owner", "rcpt-1", now=clock.t)
    assert s.settle_receipt("rcpt-1", "silent", event_ids=[1]) == "retry"
    assert s.batch(bid)["state"] == "retry"
    assert s.handled_event_ids(bid) == {1}


def test_expire_receipts_moves_expired_to_uncertain(make_store, make_config, clock):
    cfg = make_config(destinations={"out": {"type": "http", "url": "https://example.invalid/h",
                "secret": {"env": "X"}, "receipts": True,
                "receipt_dispositions": {"visible": "settled"}}})
    s = make_store(cfg)
    bid = _setup_batch(s)
    s.mark_awaiting_receipt(bid, "owner", "rcpt-1", expiry=clock.t - 1.0, now=clock.t)
    assert s.expire_receipts(clock.t) == [bid]
    assert s.batch(bid)["state"] == "uncertain"
    assert s.receipt("rcpt-1") is None
    # not-expired receipts are untouched
    bid2 = _setup_batch(s)
    s.mark_awaiting_receipt(bid2, "owner", "rcpt-2", expiry=clock.t + 100.0, now=clock.t)
    assert s.expire_receipts(clock.t) == []
    assert s.batch(bid2)["state"] == "awaiting_receipt"
    assert s.receipt("rcpt-2") is not None


def test_drop_cancels_awaiting_receipt_and_uncertain(make_store, make_config, clock):
    cfg = make_config(destinations={"out": {"type": "http", "url": "https://example.invalid/h",
                "secret": {"env": "X"}, "receipts": True}})
    s = make_store(cfg)
    bid = _setup_batch(s)
    s.mark_awaiting_receipt(bid, "owner", "rcpt-1", now=clock.t)
    assert s.drop(bid) is True
    assert s.batch(bid)["state"] == "dropped"
    bid2 = _setup_batch(s)
    s.mark_awaiting_receipt(bid2, "owner", "rcpt-2", now=clock.t)
    s.mark_uncertain(bid2, "receiver went away", now=clock.t)
    assert s.batch(bid2)["state"] == "uncertain"
    assert s.drop(bid2) is True
    assert s.batch(bid2)["state"] == "dropped"


def test_uncertain_never_auto_retried_or_dropped_by_maintenance(make_store, make_config, clock):
    cfg = make_config(destinations={"out": {"type": "http", "url": "https://example.invalid/h",
                "secret": {"env": "X"}, "receipts": True}})
    s = make_store(cfg)
    bid = _setup_batch(s)
    s.mark_awaiting_receipt(bid, "owner", "rcpt-1", now=clock.t)
    s.mark_uncertain(bid, "receiver went away", now=clock.t)
    # expire_receipts does not touch an already-uncertain batch (no receipt row remains)
    assert s.expire_receipts(clock.t + 10000.0) == []
    assert s.batch(bid)["state"] == "uncertain"


# ---- dispatch end-to-end ---------------------------------------------------------

def test_receipts_admit_then_await(rig, clock):
    r = rig({"destinations": {DEST: {"receipts": True, "receipt_dispositions": {"visible": "settled"}}}},
             start=True)
    stub = r.stub()
    stub.receipts = True
    stub.script(Outcome.ok("ok", receipt_id="rcpt-1"))
    a = r.enqueue(n=1, quiet_s=0, max_s=0)
    bid = a.batch_id
    assert r.d.drain_once() == 1
    assert r.batch(bid)["state"] == "awaiting_receipt"
    assert r.store.receipt("rcpt-1") is not None
    assert stub.calls


def test_receipts_synthesize_id_when_missing(rig, clock):
    r = rig({"destinations": {DEST: {"receipts": True}}}, start=True)
    stub = r.stub()
    stub.receipts = True
    stub.script(Outcome.ok("ok"))
    a = r.enqueue(n=1, quiet_s=0, max_s=0)
    bid = a.batch_id
    assert r.d.drain_once() == 1
    assert r.batch(bid)["state"] == "awaiting_receipt"
    assert r.store.receipts_for_batch(bid), "a receipt row should be created"
    rid = r.store.receipts_for_batch(bid)[0]["receipt_id"]
    assert rid.startswith(f"rcpt_{bid}_")


def test_without_receipts_2xx_still_delivers(rig, clock):
    r = rig(start=True)
    r.stub().script(Outcome.ok("ok"))
    a = r.enqueue(n=1, quiet_s=0, max_s=0)
    assert r.d.drain_once() == 1
    assert r.batch(a.batch_id)["state"] == "delivered"


def test_receipts_preserve_per_key_order(rig, clock):
    r = rig({"destinations": {DEST: {"receipts": True}}}, start=True)
    stub = r.stub()
    stub.receipts = True
    stub.script(Outcome.ok("ok", receipt_id="rcpt-1"))
    a = r.enqueue(key="k", n=1, quiet_s=0, max_s=0)
    bid_a = a.batch_id
    assert r.d.drain_once() == 1
    assert r.batch(bid_a)["state"] == "awaiting_receipt"
    # a second batch of the same key must not be delivered while the receipt is open
    b = r.enqueue(key="k", n=2, quiet_s=0, max_s=0)
    bid_b = b.batch_id
    r.d.drain_once()
    assert r.batch(bid_a)["state"] == "awaiting_receipt"
    assert r.batch(bid_b)["state"] != "delivered"
