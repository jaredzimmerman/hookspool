"""Tee routes (ARCHITECTURE §5, §14.1; CONFIGURATION ``[[routes]]``): a matching ``tee = true`` route
gets its own copy of the event and does not claim it. Copies are independent events in their own
batches with their own windows, all written in the intake's one transaction; dedup and ``ignore``
run once per request; ``check`` keeps one event from reaching one destination twice."""
from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys

import pytest

import kits
from _httpstub import Stub
from conftest import SENTINEL, child_env
from hookspool import config as hs_config
from hookspool import intake as I
from hookspool.__main__ import main
from hookspool.destinations import build
from hookspool.destinations.base import Outcome
from sourcekit import Env

TESTS = os.path.dirname(os.path.abspath(__file__))
SRC = "kitsigned"                                     # action hermes:kit, attrs {"repo": ...}
TEE = {"name": "widgets-dispatch", "source": SRC, "match": {"repo": "octo/widgets"}, "tee": True,
       "destination": "script:run-widgets-dispatcher", "quiet_s": 120, "max_s": 600}
CI = {"name": "widgets-ci", "source": SRC, "match": {"repo": "octo/widgets"}, "destination": "hermes:widgets-ci",
      "quiet_s": 5, "max_s": 30}
SCRIPT = {"script:run-widgets-dispatcher": {"argv": ["/usr/local/bin/run-job", "widgets-dispatcher"]}}


@pytest.fixture
def env(tmp_path, clock):
    made = []

    def make(routes=(), kit=kits.SIGNED, **over):
        e = Env(kit, tmp_path / f"e{len(made)}", clock, cfg_over={"routes": list(routes), **over})
        made.append(e)
        return e
    yield make
    for e in made:
        e.close()


def status(r) -> str:
    return json.loads(r.body)["status"]


def rows(e, q, args=()):
    return [dict(r) for r in e.store._read(q, args)]


BATCH_COLS = ("route", "source", "destination", "key", "state", "quiet_s", "max_s", "max_events", "max_bytes",
              "count", "bytes", "not_before")
EVENT_COLS = ("source", "route", "event_type", "dedup_id", "payload", "raw", "headers", "size")


def primary(e, route):
    b = rows(e, "SELECT * FROM batches WHERE route=?", (route,))
    assert len(b) == 1, b
    ev = rows(e, "SELECT * FROM events WHERE batch_id=?", (b[0]["id"],))
    return {k: b[0][k] for k in BATCH_COLS}, [{k: x[k] for k in EVENT_COLS} for x in ev]


# ---- routing ------------------------------------------------------------------------------------------------
def test_a_tee_copy_and_the_primary_are_separate_batches_and_the_primary_is_unchanged(env):
    plain, teed = env([CI]), env([TEE, CI])           # the tee is first in file order and still claims nothing
    for e in (plain, teed):
        assert status(e.send(e.signed())) == "queued"
    assert primary(teed, "widgets-ci") == primary(plain, "widgets-ci")      # golden: as if the tee were not there
    assert len(rows(plain, "SELECT * FROM batches")) == 1
    tb, tev = primary(teed, "widgets-dispatch")
    assert (tb["destination"], tb["quiet_s"], tb["max_s"], tb["state"]) == ("script:run-widgets-dispatcher", 120, 600,
                                                                            "open")
    pb, pev = primary(teed, "widgets-ci")
    assert (pb["quiet_s"], pb["max_s"]) == (5, 30)
    assert tev[0]["payload"] == pev[0]["payload"] and tev[0]["raw"] == pev[0]["raw"] == kits.SIGNED.sample.body
    ids = [r["id"] for r in rows(teed, "SELECT id FROM events ORDER BY id")]
    assert len(ids) == 2
    dd = rows(teed, "SELECT event_id FROM dedup")
    assert dd == [{"event_id": rows(teed, "SELECT e.id FROM events e JOIN batches b ON b.id = e.batch_id"
                                          " WHERE b.route='widgets-ci'")[0]["id"]}]   # dedup points at the primary


def test_a_tee_with_no_other_match_copies_and_the_source_action_still_gets_the_event(env):
    e = env([TEE])
    assert status(e.send(e.signed())) == "queued"
    got = sorted((b["route"], b["destination"]) for b in rows(e, "SELECT * FROM batches"))
    assert got == [(SRC, "hermes:kit"), ("widgets-dispatch", "script:run-widgets-dispatcher")]


def test_a_tee_that_does_not_match_makes_no_copy(env):
    e = env([dict(TEE, match={"repo": "octo/other"})])
    assert status(e.send(e.signed())) == "queued"
    assert [b["route"] for b in rows(e, "SELECT * FROM batches")] == [SRC]


def test_several_tees_copy_in_file_order(env):
    tees = [dict(TEE, name=f"t{i}", destination=f"script:t{i}", quiet_s=0, max_s=0) for i in range(3)]
    e = env([tees[2], tees[0], dict(tees[1], match={"repo": "nope"}), CI])
    assert status(e.send(e.signed())) == "queued"
    got = [(r["route"], r["destination"]) for r in rows(e, "SELECT e.route, b.destination FROM events e"
                                                           " JOIN batches b ON b.id = e.batch_id ORDER BY e.id")]
    assert got == [("widgets-ci", "hermes:widgets-ci"), ("t2", "script:t2"), ("t0", "script:t0")]


def test_resolve_tees_caps_the_copies_and_never_repeats_a_destination():
    tees = [dict(TEE, name=f"t{i}", destination=f"script:t{i}") for i in range(10)]
    cfg = hs_config.merge(hs_config.DEFAULTS, {"routes": tees + [dict(TEE, name="again", destination="script:t0"),
                                                                 dict(TEE, name="same", destination="hermes:kit")]})
    got = hs_config.resolve_tees(cfg, SRC, {"repo": "octo/widgets"}, "hermes:kit")
    assert [r for r, _ in got] == [f"t{i}" for i in range(hs_config.MAX_TEES)]
    cfg["routes"] = cfg["routes"][hs_config.MAX_TEES:]
    assert hs_config.resolve_tees(cfg, SRC, {"repo": "octo/widgets"}, "hermes:kit") == [
        ("t8", "script:t8"), ("t9", "script:t9"), ("again", "script:t0")]          # "same" is the primary's
    assert hs_config.resolve_route(cfg, SRC, {"repo": "octo/widgets"}, "hermes:kit") == (SRC, "hermes:kit")


# ---- check -----------------------------------------------------------------------------------------------------
def check(routes, **over):
    cfg = hs_config.merge(hs_config.DEFAULTS, {"sources": {"gh": {"action": "hermes:gh"}}, "routes": routes,
                                               "destinations": dict(SCRIPT, **{"script:b": {"argv": ["/bin/true"]}}),
                                               **over})
    rep = hs_config.check(cfg, load_plugin=None, check_secrets=False)
    return [e for e in rep.errors if "subscriptions_path" not in e]


def gh(**kw):
    return dict({"name": "tee", "source": "gh", "tee": True, "destination": "script:run-widgets-dispatcher"}, **kw)


def test_check_accepts_the_documented_tee():
    assert check([gh(match={"repo": "owner/widgets"}, quiet_s=120, max_s=600)]) == []
    assert check([gh(tee=False, destination="hermes:other")]) == []


@pytest.mark.parametrize("routes,want", [
    ([gh(destination="hermes:gh")], "routes.tee.destination: hermes:gh is also the source's action"),
    ([{"name": "ci", "source": "gh", "match": {"c": "ci"}, "destination": "script:b"}, gh(destination="script:b")],
     "routes.tee.destination: script:b is also the destination of route ci"),
    ([gh(destination="script:b"), {"name": "ci", "source": "gh", "destination": "script:b"}],
     "routes.tee.destination: script:b is also the destination of route ci"),
    ([gh(destination="nope")], "routes.tee.destination: unknown destination 'nope'"),
    ([gh(tee="yes")], "routes.tee.tee: must be true or false"),
    ([gh(tee=1)], "routes.tee.tee: must be true or false"),
    ([gh(destination=None)], "routes.tee.destination: required for a tee route"),
    ([gh(name="a"), gh(name="b")], "routes.b.destination: script:run-widgets-dispatcher is also the destination of"
                                   " tee route a"),
    ([gh(name=f"t{i}", destination=f"script:t{i}") for i in range(9)], "sources.gh: 9 tee routes; at most 8"),
])
def test_check_refuses(routes, want):
    routes = [{k: v for k, v in r.items() if v is not None} for r in routes]
    over = {"destinations": {f"script:t{i}": {"argv": ["/bin/true"]} for i in range(9)}}
    errs = check(routes, **over) if "tee routes" in want else check(routes)
    assert any(e.startswith(want) for e in errs), errs


def test_check_allows_two_tees_to_one_destination_when_their_matches_are_disjoint():
    assert check([gh(name="a", match={"repo": "x"}), gh(name="b", match={"repo": "y"})]) == []
    assert check([gh(name=f"t{i}", destination=f"script:t{i}") for i in range(8)],
                 destinations={f"script:t{i}": {"argv": ["/bin/true"]} for i in range(8)}) == []


def test_check_applies_the_byte_rules_to_a_tee_route():
    errs = check([gh(destination="hermes:x", max_bytes=1048576)])
    assert any(e.startswith("route tee (source gh): max_bytes 1048576 exceeds 0.75 x hermes:x") for e in errs), errs
    errs = check([gh(max_bytes=1000)], sources={"gh": {"action": "hermes:gh", "max_body_bytes": 2000}})
    assert any("source max_body_bytes 2000 exceeds the route's max_bytes 1000" in e for e in errs), errs
    cfg = hs_config.merge(hs_config.DEFAULTS, {"sources": {"gh": {"action": "hermes:gh"}}, "routes": [gh(max_bytes=1000)]})
    assert hs_config.source_max_body_bytes(cfg, "gh") == 1000          # the intake's cap fits the tee's document


# ---- once per request ---------------------------------------------------------------------------------------
def test_a_duplicate_request_makes_no_copies(env):
    e = env([TEE, CI])
    assert status(e.send(e.signed())) == "queued"
    assert status(e.send(e.signed())) == "duplicate"
    assert len(rows(e, "SELECT * FROM events")) == 2 and len(rows(e, "SELECT * FROM batches")) == 2
    assert rows(e, "SELECT dup_count FROM dedup") == [{"dup_count": 1}]


def test_a_redelivery_of_a_dead_primary_replays_it_and_makes_no_copies(env):
    e = env([TEE, CI])
    e.send(e.signed())
    pid = rows(e, "SELECT id FROM batches WHERE route='widgets-ci'")[0]["id"]
    e.store._write(lambda c: c.execute("UPDATE batches SET state='dead' WHERE id=?", (pid,)))
    assert status(e.send(e.signed())) == "replayed"
    assert e.store.batch(pid)["state"] == "retry" and len(rows(e, "SELECT * FROM events")) == 2


def test_an_ignored_request_makes_no_copies(env):
    e = env([TEE])
    r = e.send(e.signed(kits.SIGNED.sample.set("X-Kit-Event", "ping")))
    assert status(r) == "ignored" and rows(e, "SELECT * FROM events") == [] and rows(e, "SELECT * FROM batches") == []


def test_a_quarantined_request_reclassifies_into_every_copy(env):
    e = env([TEE, CI])
    e.spy.broken = "key"
    assert status(e.send(e.signed())) == "quarantined"
    assert [b["destination"] for b in rows(e, "SELECT * FROM batches")] == ["quarantine"]
    e.spy.broken = None
    qb = rows(e, "SELECT id FROM batches")[0]["id"]
    res = e.store.reclassify_quarantine(qb, I.reclassifier(e.cfg, loader=e._load))
    assert len(res) == 1 and len(res[0].tees) == 1
    got = sorted((b["route"], b["destination"]) for b in rows(e, "SELECT * FROM batches WHERE destination != 'quarantine'"))
    assert got == [("widgets-ci", "hermes:widgets-ci"), ("widgets-dispatch", "script:run-widgets-dispatcher")]


def test_source_test_reports_the_tees(env):
    e = env([TEE, CI])
    out = I.source_test(e.cfg, SRC, e.signed(), loader=e._load)
    assert out["result"] == "queued" and out["route"] == "widgets-ci"
    assert out["tees"] == [{"route": "widgets-dispatch", "destination": "script:run-widgets-dispatcher"}]
    assert e.events() == []


# ---- crash atomicity ------------------------------------------------------------------------------------------
CHILD = r"""
import json, os, signal, sys
sys.path.insert(0, sys.argv[1])
import kits
from sourcekit import Env
from hookspool import store
tmp, point = sys.argv[2], sys.argv[3]
tee = {"name": "widgets-dispatch", "source": "kitsigned", "match": {"repo": "octo/widgets"}, "tee": True,
       "destination": "script:run-widgets-dispatcher"}
tee2 = dict(tee, name="second", destination="script:second")
e = Env(kits.SIGNED, tmp, lambda: 1800000000.0, cfg_over={"routes": [tee, tee2]})
def hook(p):
    if p == point:
        os.kill(os.getpid(), signal.SIGKILL)
store._test_hook = hook
r = e.send(e.signed())
print(json.dumps({"status": r.status, "body": r.body.decode()}), flush=True)
"""


@pytest.mark.parametrize("point", ["enqueue:after_event", "enqueue:after_tee"])
def test_a_kill_between_copies_leaves_no_row_and_no_ack(tmp_path, clock, point):
    """after_event fires first after the primary's row (the first copy), after_tee after the first tee's row:
    both are inside the one intake transaction, so a kill there leaves nothing and the sender saw no 2xx."""
    d = tmp_path / "e"
    p = subprocess.run([sys.executable, "-c", CHILD, TESTS, str(d), point], env=child_env(), capture_output=True,
                       text=True, timeout=60)
    assert p.returncode == -signal.SIGKILL, p.stderr
    assert p.stdout == ""                                             # the response was never produced
    assert SENTINEL not in p.stderr
    c = sqlite3.connect(str(d / "spool.db"))
    try:
        assert c.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM dedup").fetchone()[0] == 0
    finally:
        c.close()
    # the sender retries: every copy arrives, once
    e = Env(kits.SIGNED, d, clock, cfg_over={"routes": [dict(TEE), dict(TEE, name="second", destination="script:second")]})
    try:
        assert status(e.send(e.signed())) == "queued"
        assert sorted(r["route"] for r in rows(e, "SELECT route FROM events")) == [SRC, "second", "widgets-dispatch"]
    finally:
        e.close()


# ---- delivery -------------------------------------------------------------------------------------------------
TEE_D = "script:tee"


def test_a_dead_tee_destination_never_holds_up_the_primary(rig, clock):
    r = rig()
    r.stub(TEE_D).script(*[Outcome.permanent("rejected")] * 3)
    bids = [r.store.enqueue("src", f"k{i}", "hermes:test-route", {"n": i, "note": SENTINEL}, quiet_s=0, max_s=0,
                            tees=[("tee-r", TEE_D)]) for i in range(3)]
    r.d.drain_once()
    assert [r.batch(b.batch_id)["state"] for b in bids] == ["delivered"] * 3
    assert [r.batch(b.tees[0].batch_id)["state"] for b in bids] == ["dead"] * 3


def test_an_open_breaker_on_the_tee_destination_never_holds_up_the_primary(rig, clock):
    r = rig()
    r.stub(TEE_D).script(*[Outcome.transport("connection refused")] * 50)
    made = []
    for i in range(6):
        made.append(r.store.enqueue("src", f"k{i}", "hermes:test-route", {"n": i, "note": SENTINEL}, quiet_s=0, max_s=0,
                                    tees=[("tee-r", TEE_D)]))
        r.d.drain_once()
        clock.advance(1)
    assert r.dest(TEE_D)["state"] == "open"
    assert [r.batch(b.batch_id)["state"] for b in made] == ["delivered"] * 6
    assert all(r.batch(b.tees[0].batch_id)["state"] in ("sealed", "retry") for b in made)


def test_an_unwrap_tee_sends_the_request_body_byte_for_byte(env, rig, clock, tmp_path):
    e = env([dict(TEE, destination="hermes:test-route", quiet_s=0, max_s=0)],
            destinations={"hermes:test-route": {"unwrap": True}})
    assert status(e.send(e.signed())) == "queued"
    tb = rows(e, "SELECT id FROM batches WHERE route='widgets-dispatch'")[0]["id"]
    assert e.store.stored_payloads(tb)[0][1] == kits.SIGNED.sample.body

    s = Stub()
    try:
        r = rig({"port": s.port, "destinations": {"hermes:test-route": {"unwrap": True}}}, destination_factory=build)
        payload = {"zeta": 1, "title": "café", "note": SENTINEL}
        res = r.store.enqueue("src", "k", "hermes:plain-route", payload, quiet_s=0, max_s=0,
                              tees=[("tee-r", "hermes:test-route")])
        assert r.d.drain_once() == 2
        by_path = {q["path"]: q["body"] for q in s.requests}
        assert by_path["/p/test-profile/webhooks/test-route"] == r.store.stored_payloads(res.tees[0].batch_id)[0][1]
        assert by_path["/p/test-profile/webhooks/test-route"] == json.dumps(payload, separators=(",", ":"),
                                                                           ensure_ascii=False).encode()
        assert json.loads(by_path["/webhooks/plain-route"])["batch_id"] == res.batch_id     # the primary: a document
    finally:
        s.close()


# ---- logs and the CLI -----------------------------------------------------------------------------------------
def test_one_queued_line_per_copy_and_tee_marks_the_copies(env):
    e = env([TEE, CI])
    e.send(e.signed())
    q = [r for r in e.logs.records if r.getMessage() == "queued"]
    got = [(r.route, r.destination, getattr(r, "tee", None)) for r in q]
    assert got == [("widgets-ci", "hermes:widgets-ci", None), ("widgets-dispatch", "script:run-widgets-dispatcher", True)]
    evs = {r["route"]: r for r in rows(e, "SELECT e.id, e.route, e.batch_id FROM events e")}
    assert [(r.batch, r.event) for r in q] == [(evs[x]["batch_id"], evs[x]["id"]) for x in ("widgets-ci",
                                                                                          "widgets-dispatch")]
    assert '"tee":true' in e.logs.rendered() and "tee=True" in e.logs.rendered()
    e.assert_clean_logs()
    e.send(e.signed())                                  # a duplicate: one line, no copies
    assert [r.getMessage() for r in e.logs.records].count("duplicate") == 1


def test_show_names_the_route_of_a_tee_copy(env, capsys, monkeypatch):
    e = env([TEE])
    e.send(e.signed())
    tb = rows(e, "SELECT id FROM batches WHERE route='widgets-dispatch'")[0]["id"]
    monkeypatch.setenv("HOOKSPOOL_DB", e.db)
    assert main(["show", str(tb)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["route"], out["destination"]) == ("widgets-dispatch", "script:run-widgets-dispatcher")
    assert SENTINEL not in json.dumps(out)


def test_store_refuses_a_malformed_tee(make_store):
    from hookspool.store import InvalidEvent
    st = make_store()
    for tees in ([("", "hermes:x")], [("r", "")], [("r", "quarantine")], [("r", "has space")]):
        with pytest.raises(InvalidEvent):
            st.enqueue("src", "k", "hermes:x", {"a": 1}, tees=tees)
    assert st._read("SELECT COUNT(*) FROM events")[0][0] == 0


def test_store_api_without_tees_is_unchanged(make_store):
    st = make_store()
    r = st.enqueue("src", "k", "hermes:x", {"a": 1})
    assert r.tees == () and not r.duplicate
