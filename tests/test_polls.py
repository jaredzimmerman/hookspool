"""Poll sources (ARCHITECTURE 14.4): the manifest and its checks, the expansion, the dispatcher's durable clock,
backpressure, jitter, the stale issue, the intake's 404, the schema migration, ``ctx.http.get_text`` and a poll
bobbin end to end (tick -> poller -> emits -> delivered)."""
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import child_env
from hookspool import bobbin_loader as BL
from hookspool import config as C
from hookspool import net
from hookspool import store as store_mod
from hookspool.destinations import build as build_destination
from hookspool.destinations.base import Destination, Outcome
from hookspool.destinations.script import ScriptDestination
from hookspool.dispatch import Dispatcher
from hookspool.intake import Intake
from hookspool.store import Store
from hookspool.testing import bobbinkit as K

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "pollbobbins")
T0 = 1_800_000_000.0


# ---- helpers ---------------------------------------------------------------------------------------------
@pytest.fixture
def root(tmp_path, monkeypatch):
    """A private copy of the poll fixture bobbins (0755 / 0644, owned by this user), and no built-ins."""
    r = tmp_path / "pkgs"
    shutil.copytree(FIX, r)
    for d, _dirs, files in os.walk(r):
        os.chmod(d, 0o755)
        for f in files:
            os.chmod(os.path.join(d, f), 0o644)
    os.chmod(tmp_path, 0o755)
    empty = tmp_path / "builtins"
    empty.mkdir()
    monkeypatch.setattr(BL, "BUILTIN_DIR", str(empty))
    return r


@pytest.fixture
def key_file(tmp_path):
    p = tmp_path / "feed_api_key"
    p.write_text("test-api-key-feed\n")
    os.chmod(p, 0o600)
    return str(p)


def feed_table(key_file: str, extra: str = "", outputs: str = '{ item = "script:sink" }') -> str:
    return (f'[bobbins.feed]\nbobbin = "acme.feed"\nsecrets.api_key = {{ file = "{key_file}" }}\n'
            f'settings = {{ feed = "news" }}\noutputs = {outputs}\n{extra}\n'
            '[destinations."script:sink"]\nargv = ["/bin/true"]\n')


def load(tmp_path, root, body: str) -> dict:
    p = tmp_path / "config.toml"
    p.write_text(f'[spool]\nhome = "{tmp_path / "home"}"\nbobbin_path = ["{root}"]\n[alerts]\ndry = true\n{body}')
    return C.load_config(str(p))


def errors(tmp_path, root, body: str) -> list[str]:
    with pytest.raises(BL.BobbinError) as ei:
        load(tmp_path, root, body)
    return ei.value.errors


def manifest_errors(text: str) -> list[str]:
    return BL.validate_manifest(tomllib.loads(text), "acme/feed", C.DEFAULTS["spans"])


POLL_MANIFEST = """
name = "acme.feed"
version = "1"
[poll]
every_s = 600
[outputs.item]
description = "x"
"""


def poll_cfg(every_s=600, jitter_s=0, action="hermes:ticks") -> dict:
    """A config with one poll source, as an expansion makes it."""
    cfg = C.merge(C.DEFAULTS, {"sources": {"feed": {"kind": "poll", "every_s": every_s, "jitter_s": jitter_s,
                                                    "action": action, "span": "immediate"}},
                               "alerts": {"dry": True}})
    cfg["_bobbins"] = {"feed": {"poll": True}}
    return cfg


class Sink(Destination):
    """Accepts (or, with ``fail``, refuses as a transport failure) and records batch ids."""
    type_name = "stub"

    def __init__(self, name, config, seen, fail=False):
        super().__init__(name, {"timeout_s": 5, "max_inflight": 4}, config)
        self.seen, self.fail = seen, fail

    def deliver(self, doc, body, delivery_id, ctx):
        self.seen.append((self.name, doc["batch_id"], doc["events"]))
        return Outcome.transport("down") if self.fail else Outcome.ok()


def dispatcher(tmp_path, cfg, clock, seen, *, fail=False, poll_jitter=lambda j: 0.0, store=None):
    st = store or Store(str(tmp_path / "spool.db"), cfg, now=lambda: clock[0])
    d = Dispatcher(st, cfg, now=lambda: clock[0], jitter=lambda: 0.0, lock_path=str(tmp_path / "lk"),
                   destination_factory=lambda c, n: Sink(n, c, seen, fail=fail), poll_jitter=poll_jitter)
    d.start()
    return d, st


def ticks(st: Store) -> list[dict]:
    return [dict(r) for r in st._read("SELECT * FROM events WHERE source='feed' AND event_type='poll' ORDER BY id")]


# ---- the manifest (14.4) ---------------------------------------------------------------------------------
def test_a_poll_manifest_is_valid_without_a_source():
    assert manifest_errors(POLL_MANIFEST) == []


@pytest.mark.parametrize("edit,want", [
    ('[source]\nmodule = "source"\n', "a poll bobbin has no [source]"),
    ('[processor]\nmodule = "processor"\n', "a poll bobbin has no [processor]"),
])
def test_a_poll_bobbin_takes_no_source_and_no_processor(edit, want):
    assert any(want in e for e in manifest_errors(POLL_MANIFEST + edit))


@pytest.mark.parametrize("poll,want", [
    ("every_s = 59", "poll.every_s: must be a number of seconds >= 60"),
    ("every_s = true", "poll.every_s: must be a number"),
    ("jitter_s = 10", "poll.every_s: required"),
    ("every_s = 600\njitter_s = 301", "poll.jitter_s: must be a number of seconds from 0 to 300"),
    ("every_s = 600\njitter_s = -1", "poll.jitter_s"),
    ('every_s = 600\nmodule = "worker"', 'poll.module: must be "poller"'),
    ("every_s = 600\ntimeout_s = 0", "poll.timeout_s: must be a number > 0"),
    ('every_s = 600\nstate = "yes"', "poll.state: must be true or false"),
    ("every_s = 600\nbogus = 1", "poll.bogus: unknown key"),
])
def test_poll_table_errors(poll, want):
    m = f'name = "acme.feed"\nversion = "1"\n[poll]\n{poll}\n[outputs.item]\ndescription = "x"\n'
    errs = manifest_errors(m)
    assert any(want in e for e in errs), errs


def test_neither_source_nor_poll_names_both():
    errs = manifest_errors('name = "acme.feed"\nversion = "1"\n[outputs.x]\ndescription = "x"\n')
    assert any("source: required (or [poll]" in e for e in errs)


def test_a_poll_bobbin_without_its_poller_is_refused(tmp_path, root):
    os.remove(root / "acme" / "feed" / "poller.py")
    pkgs, errs = BL.discover(C.merge(C.DEFAULTS, {"spool": {"bobbin_path": [str(root)]}}))
    assert any("poll: poller.py is not in the bobbin" in e for e in errs)


# ---- the expansion (14.4) ---------------------------------------------------------------------------------
def test_a_poll_instance_expands_to_a_poll_source_and_a_poller_destination(tmp_path, root, key_file):
    cfg = load(tmp_path, root, feed_table(key_file))
    src = cfg["sources"]["feed"]
    assert src == {"kind": "poll", "every_s": 600, "jitter_s": 60.0, "span": "immediate", "action": "script:feed",
                   "secrets": {"api_key": {"file": key_file}}}
    d = cfg["destinations"]["script:feed"]
    assert d["processor"] == "feed" and d["max_inflight"] == 1 and d["timeout_s"] == 60
    assert d["state_dir"] == str(tmp_path / "home" / "bobbins" / "feed")
    # a poll bobbin has no source, so every secret it declares is its poller's
    assert json.loads(d["env"]["HOOKSPOOL_SECRET_REFS"]) == {"api_key": {"file": key_file}}
    assert json.loads(d["env"]["HOOKSPOOL_DELTA_ROUTES"]) == {"item": "script:sink"}
    assert cfg["_bobbins"]["feed"]["poll"] is True and cfg["routes"] == []
    assert C.check(cfg).ok, C.check(cfg).errors
    assert C.poll_sources(cfg) == {"feed": C.PollSpec(600.0, 60.0, "script:feed")}


def test_the_instance_overrides_every_s_and_jitter_s_and_the_default_jitter_is_capped(tmp_path, root, key_file):
    cfg = load(tmp_path, root, feed_table(key_file, "every_s = 3600\njitter_s = 5"))
    assert (cfg["sources"]["feed"]["every_s"], cfg["sources"]["feed"]["jitter_s"]) == (3600, 5)
    cfg = load(tmp_path, root, feed_table(key_file, "every_s = 86400"))
    assert cfg["sources"]["feed"]["jitter_s"] == 300.0                    # 10% of a day, at most 300 s
    assert BL.default_jitter_s(600) == 60.0 and BL.default_jitter_s(100000) == 300.0


@pytest.mark.parametrize("extra,want", [
    ("every_s = 30", "bobbins.feed.every_s: must be a number of seconds >= 60"),
    ("jitter_s = 900", "bobbins.feed.jitter_s: must be a number of seconds from 0 to 300"),
    ('span = "burst"', "bobbins.feed.span: bobbin 'acme.feed' polls"),
    ("expect = true", "bobbins.feed.expect: bobbin 'acme.feed' polls"),
    ("max_body_bytes = 100", "bobbins.feed.max_body_bytes: bobbin 'acme.feed' polls"),
])
def test_poll_instance_errors(tmp_path, root, key_file, extra, want):
    errs = errors(tmp_path, root, feed_table(key_file, extra))
    assert any(want in e for e in errs), errs


def test_a_webhook_bobbin_instance_takes_no_poll_keys(tmp_path, root, key_file):
    hook = root / "acme" / "relay"
    hook.mkdir()
    (hook / "bobbin.toml").write_text('name = "acme.relay"\nversion = "1"\n[source]\n'
                                      'template = { scheme = "url-token", key = "id" }\n[secrets.secret]\n'
                                      'description = "x"\n[outputs."*"]\ndescription = "all"\n')
    body = (f'[bobbins.relay]\nbobbin = "acme.relay"\nsecrets.secret = {{ file = "{key_file}" }}\n'
            'outputs = { "*" = "hermes:x" }\nevery_s = 600\n')
    assert any("bobbins.relay.every_s: only for a poll bobbin" in e for e in errors(tmp_path, root, body))


def test_check_refuses_a_hand_written_poll_source_and_poll_keys_on_a_webhook_source():
    cfg = poll_cfg()
    cfg["_bobbins"] = {}
    rep = C.check(cfg, load_plugin=None, check_secrets=False)
    assert any('sources.feed.kind: "poll" is set only by a poll bobbin' in e for e in rep.errors), rep.errors
    cfg = poll_cfg(every_s=10)
    cfg["sources"]["feed"]["keep_headers"] = ["X-A"]
    rep = C.check(cfg, load_plugin=None, check_secrets=False)
    assert any("sources.feed.every_s: must be a number of seconds >= 60" in e for e in rep.errors)
    assert any("sources.feed.keep_headers: not a key of a poll source" in e for e in rep.errors)
    cfg = C.merge(C.DEFAULTS, {"sources": {"x": {"module": "m", "action": "hermes:x", "every_s": 600,
                                                 "kind": "pull"}}})
    rep = C.check(cfg, load_plugin=None, check_secrets=False)
    assert any("sources.x.every_s: only for a poll source" in e for e in rep.errors)
    assert any("sources.x.kind: 'pull' is not one of webhook, poll" in e for e in rep.errors)


# ---- the intake: a poll source has no URL --------------------------------------------------------------------
def test_the_intake_answers_404_for_a_poll_source(tmp_path, root, key_file):
    cfg = load(tmp_path, root, feed_table(key_file))
    st = Store(str(tmp_path / "spool.db"), cfg)
    st.ensure_schema()
    intake = Intake(cfg, st)
    resp = intake.handle("POST", "/hooks/feed", [("Content-Type", "application/json"), ("Content-Length", "2")],
                         __import__("io").BytesIO(b"{}"), "127.0.0.1")
    assert resp.status == 404
    assert st._read("SELECT COUNT(*) FROM events")[0][0] == 0
    from hookspool.intake import source_test
    from hookspool.plugin import PluginError, RawRequest
    with pytest.raises(PluginError, match="poll source"):
        source_test(cfg, "feed", RawRequest("POST", "/hooks/feed", [], b"{}", {}))


# ---- the clock (store level) ------------------------------------------------------------------------------------
@pytest.fixture
def st(tmp_path):
    s = Store(str(tmp_path / "spool.db"), poll_cfg(), now=lambda: T0)
    s.ensure_schema()
    yield s
    s.close()


def test_a_new_instance_is_scheduled_then_ticks_once_per_window(st):
    r = st.poll_tick("feed", "hermes:ticks", 600, 0, now=T0)
    assert r.action == "scheduled" and r.next_at == T0
    r = st.poll_tick("feed", "hermes:ticks", 600, 0, now=T0 + 1)
    assert r.action == "enqueued" and r.next_at == T0 + 601
    ev = ticks(st)
    assert len(ev) == 1 and json.loads(ev[0]["payload"]) == {"tick": int(T0 + 1), "instance": "feed"}
    assert ev[0]["dedup_id"] == f"poll:feed:{T0:.3f}"
    b = st.batch(r.batch_id)
    assert (b["key"], b["destination"], b["quiet_s"], b["max_s"]) == ("poll", "hermes:ticks", 0, 0)
    assert st.poll_tick("feed", "hermes:ticks", 600, 0, now=T0 + 300).action == "not_due"
    assert len(ticks(st)) == 1


def test_a_missed_window_yields_one_tick_never_a_burst(st):
    st.poll_tick("feed", "hermes:ticks", 600, 0, now=T0)
    # the dispatcher was down for ten windows
    r = st.poll_tick("feed", "hermes:ticks", 600, 0, now=T0 + 6000)
    assert r.action == "enqueued" and r.next_at == T0 + 6600
    assert st.poll_tick("feed", "hermes:ticks", 600, 0, now=T0 + 6001).action == "not_due"
    assert len(ticks(st)) == 1


def test_an_undelivered_tick_holds_back_the_next_one(st):
    st.poll_tick("feed", "hermes:ticks", 600, 0, now=T0)
    first = st.poll_tick("feed", "hermes:ticks", 600, 0, now=T0)
    assert first.action == "enqueued"
    r = st.poll_tick("feed", "hermes:ticks", 600, 0, now=T0 + 600)        # the first is still pending
    assert r.action == "skipped" and r.next_at == T0 + 1200 and r.batch_id == first.batch_id
    assert len(ticks(st)) == 1
    assert st.poll_rows()["feed"]["skipped"] == 1
    # a dead tick holds it back too, until an operator replays or drops it
    st.seal_ready(T0 + 1200)
    assert st.claim(first.batch_id, 60, "w", now=T0 + 1200)
    a = st.start_attempt(first.batch_id, "w", now=T0 + 1200)
    st.mark_dead(first.batch_id, "w", "refused", "permanent", attempt_id=a.id, now=T0 + 1200)
    assert st.poll_tick("feed", "hermes:ticks", 600, 0, now=T0 + 1200).action == "skipped"
    st.drop(first.batch_id, now=T0 + 1800)
    assert st.poll_tick("feed", "hermes:ticks", 600, 0, now=T0 + 1800).action == "enqueued"
    assert len(ticks(st)) == 2


def test_a_tick_whose_dedup_id_is_already_there_adds_nothing(st):
    """If a tick were committed without its next_at update, the next pass reuses the scheduled time, and the
    dedup id makes the second enqueue a duplicate: still exactly one tick."""
    st.poll_tick("feed", "hermes:ticks", 600, 0, now=T0)
    first = st.enqueue("feed", "poll", "hermes:ticks", {"tick": int(T0), "instance": "feed"}, event_type="poll",
                       dedup_id=store_mod.poll_dedup_id("feed", T0), quiet_s=0, max_s=0)
    r = st.poll_tick("feed", "hermes:ticks", 600, 0, now=T0 + 5)
    assert r.action == "duplicate" and r.batch_id == first.batch_id and r.next_at == T0 + 605
    assert len(ticks(st)) == 1
    assert st.poll_rows()["feed"]["last_batch_id"] == first.batch_id


KILL_CHILD = r"""
import os, signal, sys
from hookspool import store, config
db = sys.argv[1]
cfg = config.merge(config.DEFAULTS, {})
st = store.Store(db, cfg, now=lambda: 1800000000.0)
def hook(p):
    if p == "poll:after_enqueue":
        os.kill(os.getpid(), signal.SIGKILL)
store._test_hook = hook
st.poll_tick("feed", "hermes:ticks", 600, 0, now=1800000000.0)          # scheduled (no hook)
st.poll_tick("feed", "hermes:ticks", 600, 0, now=1800000001.0)          # killed between enqueue and next_at
"""


def test_a_kill_between_the_enqueue_and_the_next_at_update_leaves_exactly_one_tick(tmp_path):
    db = str(tmp_path / "spool.db")
    Store(db, poll_cfg()).ensure_schema()
    p = subprocess.run([sys.executable, "-c", KILL_CHILD, db], env=child_env(), capture_output=True, text=True,
                       timeout=60)
    assert p.returncode == -signal.SIGKILL, p.stderr
    c = sqlite3.connect(db)
    assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0          # the whole transaction rolled back
    assert c.execute("SELECT next_at, last_tick_at FROM polls").fetchone() == (T0, None)
    c.close()
    # the restarted dispatcher ticks the same window once
    clock, seen = [T0 + 2], []
    d, st = dispatcher(tmp_path, poll_cfg(), clock, seen)
    try:
        d.drain_once()
        clock[0] += 10
        d.drain_once()
    finally:
        d.close()
    assert len(ticks(st)) == 1 and ticks(st)[0]["dedup_id"] == f"poll:feed:{T0:.3f}"
    assert [s[0] for s in seen] == ["hermes:ticks"]


def test_the_schedule_survives_a_restart(tmp_path):
    clock, seen = [T0], []
    d, st = dispatcher(tmp_path, poll_cfg(), clock, seen)
    d.drain_once()                       # scheduled
    clock[0] += 1
    d.drain_once()                       # the first tick, delivered
    d.close()
    nxt = st.poll_rows()["feed"]["next_at"]
    assert nxt == T0 + 601 and len(seen) == 1
    clock[0] = T0 + 300                  # restarted before the next window: nothing
    d, st = dispatcher(tmp_path, poll_cfg(), clock, seen, store=st)
    d.drain_once()
    assert len(seen) == 1 and st.poll_rows()["feed"]["next_at"] == nxt
    clock[0] = nxt
    d.drain_once()
    d.close()
    assert len(seen) == 2 and len(ticks(st)) == 2


def test_jitter_stays_within_its_bounds(tmp_path):
    import random
    rnd = random.Random(7)
    clock, seen = [T0], []
    cfg = poll_cfg(every_s=600, jitter_s=60)
    d, st = dispatcher(tmp_path, cfg, clock, seen, poll_jitter=lambda j: rnd.uniform(0.0, j))
    try:
        d.drain_once()
        first = st.poll_rows()["feed"]["next_at"]
        assert T0 <= first <= T0 + 60                                       # a new instance: within the jitter
        offsets = []
        for _ in range(30):
            clock[0] = st.poll_rows()["feed"]["next_at"]
            d.drain_once()
            offsets.append(st.poll_rows()["feed"]["next_at"] - clock[0])
        assert all(600 <= o <= 660 for o in offsets), offsets
        assert len({round(o, 3) for o in offsets}) > 1                     # it does vary
        assert len(ticks(st)) == 30
    finally:
        d.close()
    # a draw outside the bounds is clamped to them
    s2 = Store(str(tmp_path / "s2.db"), cfg, now=lambda: T0)
    s2.ensure_schema()
    s2.poll_tick("feed", "hermes:t", 600, 60, now=T0, jitter=lambda j: 1e9)
    assert s2.poll_rows()["feed"]["next_at"] == T0 + 60
    assert s2.poll_tick("feed", "hermes:t", 600, 60, now=T0 + 60, jitter=lambda j: -5).next_at == T0 + 660


def test_a_shorter_every_s_after_a_reload_reschedules(st):
    st.poll_tick("feed", "hermes:ticks", 86400, 0, now=T0)
    st.poll_tick("feed", "hermes:ticks", 86400, 0, now=T0)                  # next_at a day out
    r = st.poll_tick("feed", "hermes:ticks", 600, 0, now=T0 + 10)
    assert r.action == "rescheduled" and r.next_at == T0 + 610


def test_backpressure_in_the_dispatcher_a_failing_poller_never_piles_up_ticks(tmp_path):
    clock, seen = [T0], []
    d, st = dispatcher(tmp_path, poll_cfg(every_s=60), clock, seen, fail=True)
    try:
        for _ in range(20):
            d.drain_once()
            clock[0] += 60
    finally:
        d.close()
    assert len(ticks(st)) == 1                                              # retried, never joined by more ticks
    assert st.poll_rows()["feed"]["skipped"] >= 15
    assert len({b for _n, b, _e in seen}) == 1


def test_a_poll_with_no_delivered_tick_for_three_windows_opens_a_stale_issue(tmp_path):
    clock, seen = [T0], []
    cfg = poll_cfg(every_s=600)
    d, st = dispatcher(tmp_path, cfg, clock, seen, fail=True)
    try:
        d.drain_once()
        for _ in range(3 * 10 + 2):                                       # minute by minute, past 3 x 600 s
            clock[0] += 60
            d.drain_once()
        issue = st.issue_get("poll:feed:stale")
        assert issue is not None and issue["state"] == "open" and issue["kind"] == "source_silent"
        assert "no tick delivered for longer than 3 x 600s (last: never)" in issue["last_error"]
        status = st.status(clock[0])
        assert status["polls"]["feed"]["stale"] is True and status["polls"]["feed"]["pending"] is True
        assert any(x.startswith("poll feed stale") for x in status["degraded"])
    finally:
        d.close()
    # the destination recovers: the tick is delivered and the issue resolves
    d, st = dispatcher(tmp_path, cfg, clock, seen, store=st)
    try:
        for _ in range(40):
            clock[0] += 60
            d.drain_once()
        assert st.issue_get("poll:feed:stale")["state"] == "resolved"
        p = st.status(clock[0])["polls"]["feed"]
        assert p["stale"] is False and p["delivered_at"] is not None and p["delivered_tick_at"] is not None
        assert p["next_at"] > clock[0] and p["every_s"] == 600.0
    finally:
        d.close()


def test_status_shows_each_poll_instance(st):
    st.poll_tick("feed", "hermes:ticks", 600, 0, now=T0)
    s = st.status(T0 + 30)["polls"]["feed"]
    assert s["next_at"] == T0 and s["next_in_s"] == -30.0 and s["delivered_at"] is None and s["stale"] is False


# ---- the schema --------------------------------------------------------------------------------------------------
def test_a_schema_2_spool_migrates_to_3_and_gains_the_polls_table(tmp_path):
    p = str(tmp_path / "old.db")
    c = sqlite3.connect(p)
    for _n, path in store_mod.migrations()[:2]:
        for stmt in store_mod._split_sql(open(path, encoding="utf-8").read()):
            c.execute(stmt)
    c.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '2')")
    c.execute("PRAGMA user_version=2")
    c.execute("INSERT INTO batches (route, source, destination, key, state, first_at, last_at, quiet_s, max_s,"
              " max_events, max_bytes) VALUES ('r', 's', 'hermes:r', 'k', 'open', 1, 1, 0, 0, 1, 1)")
    c.commit()
    c.close()
    s = Store(p, poll_cfg())
    with pytest.raises(store_mod.SchemaError, match="older than 3"):
        s.check_schema()
    assert s.ensure_schema() == [3] and s.check_schema() == 3
    assert s.poll_rows() == {} and len(s.list(limit=10)) == 1
    assert s.poll_tick("feed", "hermes:t", 600, 0, now=T0).action == "scheduled"


# ---- ctx.http.get_text ------------------------------------------------------------------------------------------
class _Feed:
    """A loopback feed with an ETag: a matching If-None-Match gets 304."""

    def __init__(self):
        self.requests: list[dict] = []
        self.etag = '"v1"'
        self.body = json.dumps({"items": [{"id": "i1"}, {"id": "i2"}]}).encode()
        feed = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                feed.requests.append({"path": self.path, "headers": dict(self.headers.items())})
                if self.path.startswith("/missing"):
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.headers.get("If-None-Match") == feed.etag:
                    self.send_response(304)
                    self.send_header("ETag", feed.etag)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("ETag", feed.etag)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(feed.body)))
                self.end_headers()
                self.wfile.write(feed.body)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def feed():
    f = _Feed()
    yield f
    f.close()


def test_get_text_returns_the_body_and_headers_and_a_304_without_raising(feed):
    h = net.Http(timeout_s=5)
    status, text, hdrs = h.get_text(feed.url + "/items", params={"feed": "news"})
    assert status == 200 and json.loads(text)["items"][0]["id"] == "i1"
    assert hdrs["etag"] == '"v1"' and hdrs["content-type"].startswith("application/json")
    assert feed.requests[-1]["path"] == "/items?feed=news"
    status, text, hdrs = h.get_text(feed.url + "/items", {"If-None-Match": '"v1"'}, bearer="test-token")
    assert (status, text, hdrs["etag"]) == (304, "", '"v1"')
    assert feed.requests[-1]["headers"]["Authorization"] == "Bearer test-token"
    with pytest.raises(net.Permanent, match="HTTP 404"):
        h.get_text(feed.url + "/missing")


def test_get_text_keeps_the_size_cap_and_the_charset():
    assert net._charset("text/xml; charset=ISO-8859-1") == "iso8859-1"
    assert net._charset("application/json") == "utf-8" and net._charset("text/plain; charset=nonsense") == "utf-8"


def test_get_text_refuses_an_oversize_answer(feed):
    with pytest.raises(net.Permanent, match="larger than"):
        net.Http(timeout_s=5, max_response_bytes=10).get_text(feed.url + "/items")


# ---- the kit and the scaffold ----------------------------------------------------------------------------------
def test_the_fixture_poll_bobbin_passes_the_kit(root):
    card = K.check_bobbin(str(root / "acme" / "feed"))
    assert card.ok, K.render_text(card.as_dict())
    by = {c.name: c for c in card.checks}
    assert by["processor"].result == "pass" and "journal replayed" in by["processor"].detail
    assert by["source-kit"].result == "skip" and by["captures"].result == "pass"


def test_the_kit_holds_a_poll_bobbin_to_its_own_rules(root):
    d = root / "acme" / "feed"
    m = (d / "bobbin.toml").read_text()
    (d / "bobbin.toml").write_text(m.replace('no_push_docs = "https://docs.github.com/en/rest/activity/feeds"\n', "")
                                   .replace('homepage = "https://github.com"\n',
                                            'homepage = "https://github.com"\nwebhook_docs = "https://docs.github.com/en/webhooks"\n'))
    (d / "poller.py").write_text((d / "poller.py").read_text().replace('"event_type": "item"', '"event_type": "other"'))
    (d / "captures" / "stray.http").write_text("POST /hooks/feed HTTP/1.1\nContent-Length: 2\n\n{}\n")
    card = K.check_bobbin(str(d))
    by = {c.name: c for c in card.checks}
    msgs = [f.message for c in card.checks for f in c.findings]
    assert by["provider"].result == "fail"
    assert any("provider.no_push_docs: required" in x for x in msgs)
    assert any("provider.webhook_docs: a poll bobbin receives no webhooks" in x for x in msgs)
    assert any("not declared outputs" in x for x in msgs)
    assert any("a poll bobbin receives no requests" in x for x in msgs)


def test_bobbin_new_poll_scaffolds_a_bobbin_that_fails_only_on_its_placeholders(tmp_path):
    from hookspool.__main__ import main
    parent = tmp_path / "out"
    parent.mkdir()
    os.chmod(tmp_path, 0o755)
    assert main(["bobbin", "new", "acme.tasks", "--poll", "--dir", str(parent)]) == 0
    d = parent / "acme" / "tasks"
    assert sorted(os.listdir(d)) == ["AGENTS.md", "README.md", "SETUP.md", "bobbin.toml", "captures", "poller.py"]
    assert sorted(os.listdir(d / "captures")) == ["kit.toml", "poll-example.json"]
    m = tomllib.loads((d / "bobbin.toml").read_text())
    assert "source" not in m and m["poll"]["every_s"] == BL.POLL_SCAFFOLD_EVERY_S
    card = K.check_bobbin(str(d))
    assert sorted(c.name for c in card.failed) == ["provider", "todos"], K.render_text(card.as_dict())
    assert {c.name: c.result for c in card.checks}["processor"] == "pass"
    assert main(["bobbin", "new", "acme.other", "--poll", "--processor", "--dir", str(parent)]) == 2


# ---- end to end: tick -> poller -> emits -> delivered -----------------------------------------------------------
def test_a_poll_bobbin_end_to_end(tmp_path, root, key_file, feed, monkeypatch):
    cfg = load(tmp_path, root, feed_table(key_file, outputs='{ item = "hermes:feed-item" }')
               .replace('settings = { feed = "news" }', f'settings = {{ feed = "news", url = "{feed.url}/items" }}'))
    # the processor child imports this checkout (the dispatcher's own interpreter, as -m hookspool.processor does)
    code = f"import sys; sys.path.insert(0, {ROOT!r}); from hookspool import processor; sys.exit(processor.main())"
    monkeypatch.setattr(ScriptDestination, "command",
                        lambda self: [sys.executable, "-I", "-c", code, self.processor] if self.processor
                        else list(self.argv))
    clock, seen = [T0], []

    def factory(c, name):
        return build_destination(c, name) if name == "script:feed" else Sink(name, c, seen)
    st = Store(str(tmp_path / "spool.db"), cfg, now=lambda: clock[0])
    d = Dispatcher(st, cfg, now=lambda: clock[0], jitter=lambda: 0.0, lock_path=str(tmp_path / "lk"),
                   destination_factory=factory, poll_jitter=lambda j: 0.0)
    d.start()
    try:
        d.drain_once()                                         # scheduled at T0
        clock[0] += 1
        d.drain_once()                                         # tick -> poller -> two items -> delivered
        assert [(n, [e["id"] for e in evs]) for n, _b, evs in seen] == [("hermes:feed-item", ["i1"]),
                                                                       ("hermes:feed-item", ["i2"])]
        assert st.list("delivered", source="feed", destination="script:feed")
        assert feed.requests[-1]["headers"]["Authorization"] == "Bearer test-api-key-feed"
        state = json.loads((tmp_path / "home" / "bobbins" / "feed" / "cursor.json").read_text())
        assert state == {"etag": '"v1"', "seen": ["i1", "i2"]}
        # the next window: a conditional GET, a 304, nothing new
        clock[0] = st.poll_rows()["feed"]["next_at"]
        d.drain_once()
        assert len(seen) == 2 and feed.requests[-1]["headers"]["If-None-Match"] == '"v1"'
        assert len(ticks(st)) == 2
        p = st.status(clock[0])["polls"]["feed"]
        assert p["delivered_tick_at"] is not None and p["stale"] is False and p["pending"] is False
    finally:
        d.close()
