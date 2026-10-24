"""The ``drive`` bobbin (ARCHITECTURE 14.3): its source (channel token, ``sync`` ping, key, dedup) through the shared
source kit and its captures, and its processor through the processor runtime (journal, staged state, emits) against a
scripted Google (``tests/_googlestub.py``):

* the page-token loop: bootstrap, advance, replay of a retried batch, an expired token, paging;
* filtering: the account's own edits, the trash, MIME types, removals; one item per file;
* naming: ``file-changed`` / ``file-created`` / ``file-shared`` and the fields an item carries (never contents);
* failures: ``Retryable`` (exit 75) / ``Permanent`` (exit 65), and that a failed pass changes no state;
* the 14.3.7 rules and the documents of 14.3.8.
"""
from __future__ import annotations

import json
import os
import re

import pytest

import _bobbinrig
import _bobbinrules as R
import _googlestub
import sourcekit
from sourcekit import SENTINEL, Env, Kit, request

from hookspool import bobbin_loader as PK
from hookspool import config as C
from hookspool import plugin as P
from hookspool.bobbins.google.drive import processor as dp
from hookspool.bobbins.google.drive import source as drive
from hookspool.plugin import Request, parse_capture
from hookspool.providers import google as G

OWNER = "owner@example.test"
OTHER = "colleague@example.test"
OUTPUTS = dp.OUTPUTS
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DIR = os.path.join(ROOT, "hookspool", "bobbins", "google", "drive")
TOKEN = "test-drive-channel-token-" + SENTINEL
URI = "https://www.googleapis.com/drive/v3/changes?alt=json&pageToken=100-" + SENTINEL
HW = "2026-10-01T10:00:00.000Z"                    # the newest change time the previous pass saw
BEFORE = "2026-09-01T09:00:00.000Z"
AFTER = "2026-10-01T11:00:00.000Z"
LATER = "2026-10-01T12:00:00.000Z"


# ---- the source -------------------------------------------------------------------------------------------
def push_headers(state="change", channel="chan-1", number="7", resource="res-1", uri=URI) -> dict:
    h = {"X-Goog-Channel-ID": channel, "X-Goog-Resource-ID": resource, "X-Goog-Resource-State": state,
         "X-Goog-Message-Number": number, "User-Agent": "APIs-Google", "Content-Length": "0",
         "X-Goog-Resource-URI": uri}
    return {k: v for k, v in h.items() if v is not None}


def sign(raw, secrets, now):
    return raw.set("X-Goog-Channel-Token", secrets["channel_token"])


KIT = Kit(name="drive", module=drive, secrets={"channel_token": TOKEN}, sign=sign, body_signed=False,
          source_cfg={"action": "script:drive"}, sample=request("POST", "/hooks/drive", push_headers()))


@pytest.mark.parametrize("case", sourcekit.CASES, ids=sourcekit.CASE_IDS)
def test_kit_matrix(case, tmp_path, clock):
    sourcekit.run(case, KIT, tmp_path, clock)


CAPTURES = sourcekit.captures("drive")


@pytest.mark.parametrize("path", CAPTURES, ids=[os.path.basename(p) for p in CAPTURES])
def test_captures(path, tmp_path, clock):
    assert sourcekit.run_capture(path, KIT, tmp_path, clock)["source"] == "drive"


def test_captures_cover_the_outcomes():
    got = {}
    for c in CAPTURES:
        with open(c, "rb") as f:
            got[os.path.basename(c)] = parse_capture(f.read()).meta["expect"]
    assert got.get("event.http") == "queued" and got.get("sync.http") == "ignored", got
    assert got.get("wrong_token.http") == "unverified" and got.get("no_token.http") == "unverified", got


@pytest.fixture
def env(tmp_path, clock):
    e = Env(KIT, tmp_path, clock)
    yield e
    e.close()


def post(env, token=TOKEN, **hdr):
    raw = request("POST", "/hooks/drive", push_headers(**hdr))
    if token is not None:
        raw = raw.set("X-Goog-Channel-Token", token)
    return env.send(raw)


def status(r):
    return r.status, (json.loads(r.body)["status"] if r.body else "")


def test_wrong_missing_and_empty_tokens_are_401(env):
    for tok in ("nope", None, "", TOKEN[:-1], TOKEN + "x"):
        assert (post(env, token=tok).status, post(env, token=tok).body) == (401, b""), tok
    assert env.events() == []


def test_a_sync_ping_is_verified_then_acknowledged_and_not_enqueued(env):
    assert status(post(env, state="sync", number="1")) == (200, "ignored")
    assert env.batches() == [] and env.events() == []
    assert post(env, state="sync", token="nope").status == 401


def classify(**hdr):
    h = list(push_headers(**hdr).items())
    return P.Source("drive", drive, {}).classify(Request("POST", "/hooks/drive", h, b"", now=0))


def test_payload_is_built_from_the_headers():
    c = classify()
    assert c.payload == {"event_type": "change", "channel_id": "chan-1", "resource_id": "res-1",
                         "resource_state": "change", "resource_uri": URI, "message_number": "7",
                         "source": "google-drive"}
    assert c.event_type == "change" and c.attrs == {"state": "change"} and c.default_action is None


def test_key_is_the_watched_resource_then_the_channel():
    assert classify(resource="res-1").key == "res-1"
    assert classify(resource="", channel="chan-9").key == "chan-9"
    assert classify(resource="", channel="").key == "changes"


def test_dedup_is_per_channel_message_number():
    assert classify(channel="c1", number="7").dedup_id == "c1:7"
    assert classify(channel="c2", number="7").dedup_id == "c2:7"
    assert classify(channel="", number="7").dedup_id is None


def test_a_resent_message_number_is_a_duplicate_but_another_channel_is_not(env):
    assert status(post(env, channel="c1", number="7")) == (200, "queued")
    assert status(post(env, channel="c1", number="7")) == (200, "duplicate")
    assert status(post(env, channel="c2", number="7")) == (200, "queued")


def test_pushes_of_one_resource_are_one_batch_on_the_calendar_span(env):
    for i in range(3):
        assert status(post(env, number=str(i + 1))) == (200, "queued")
    b = env.batches()
    assert len(b) == 1 and b[0]["key"] == "res-1" and b[0]["count"] == 3
    assert b[0]["destination"] == "script:drive" and (b[0]["quiet_s"], b[0]["max_s"]) == (45.0, 180.0)


def test_nothing_is_stored_but_the_derived_payload(env):
    post(env, number="9")
    assert env.events()[0]["raw"] in (None, b"")
    env.assert_no_secret_in_db()


def test_declarations():
    assert (drive.span, drive.sender_retries) == ("calendar", True)
    assert not hasattr(drive, "action") and drive.auth_headers == ("X-Goog-Channel-Token",)


# ---- the processor: fixtures ------------------------------------------------------------------------------
@pytest.fixture
def google():
    g = _googlestub.GoogleStub()
    yield g
    g.close()


@pytest.fixture
def make(tmp_path, google):
    """``make(**settings)`` -> a Rig for the google.drive bobbin."""
    def build_rig(**over):
        cred = _bobbinrig.write_cred(tmp_path / "creds" / "drive.json", google.port)
        settings = {"own": [OWNER], **google.drive_settings(), **over}
        return _bobbinrig.Rig(tmp_path, "google.drive", OUTPUTS, settings, {"cred": cred})
    return build_rig


def fil(fid, name="Plan", mime="application/vnd.google-apps.document", created=BEFORE, modified=AFTER, by=OTHER,
        trashed=False, shared_at=None, sharer=None):
    f = {"id": fid, "name": name, "mimeType": mime, "createdTime": created, "modifiedTime": modified,
         "webViewLink": f"https://drive.example.test/file/{fid}", "trashed": trashed,
         "lastModifyingUser": {"emailAddress": by, "displayName": "Someone"}}
    if shared_at:
        f["sharedWithMeTime"] = shared_at
    if sharer:
        f["sharingUser"] = {"emailAddress": sharer}
    return f


def chg(f, time=AFTER):
    return {"fileId": f["id"], "removed": False, "time": time, "file": f}


def feed(*changes, token="start-2"):
    return {"changes": list(changes), "newStartPageToken": token}


def push(state="change", n=1):
    return {"event_type": state, "channel_id": "c1", "resource_id": "r", "resource_state": state,
            "resource_uri": URI, "message_number": str(n), "source": "google-drive"}


def seed(rig, token="p0", high_water=HW):
    rig.seed_state(dp.STATE, {"pageToken": token, "high_water": high_water})


def kinds(items):
    return [i["event_type"] for i in items]


# ---- the page-token loop ----------------------------------------------------------------------------------
def test_first_push_bootstraps_a_start_token_and_says_nothing(make, google):
    rig = make()
    google.drive_start = "start-77"
    assert rig.items(push()) == []
    st = rig.get_state(dp.STATE)
    assert st["pageToken"] == "start-77" and st["high_water"] and "bootstrapped" in st
    assert google.count("GET", "/drive/changes/startPageToken") == 1 and google.count("GET", "/drive/changes") == 1


def test_a_sync_ping_with_a_stored_token_keeps_it_and_reads_the_feed(make, google):
    rig = make()
    seed(rig, "p0")
    google.drive_start = "start-fresh"
    google.drive_pages = [feed(chg(fil("f1")), token="p1")]
    assert kinds(rig.items(push("sync"))) == ["file-changed"], "changes since the stored token are not skipped"
    assert rig.get_state(dp.STATE)["pageToken"] == "p1"
    assert google.count("GET", "/drive/changes/startPageToken") == 0


def test_a_sync_ping_without_a_token_bootstraps(make, google):
    rig = make()
    google.drive_start = "start-fresh"
    assert rig.items(push("sync")) == []
    assert rig.get_state(dp.STATE)["pageToken"] == "start-fresh"
    assert google.count("GET", "/drive/changes") == 1, "only the start token was asked for"


def test_a_bootstrap_without_a_token_is_retried(make, google):
    rig = make()
    google.drive_start = ""
    rc, emits, err = rig.run(push())
    assert rc == 75 and emits == [] and not rig.has_state(dp.STATE)


def test_the_page_token_advances_and_the_next_batch_starts_from_it(make, google):
    rig = make()
    seed(rig, "p0")
    google.drive_pages = [feed(chg(fil("f1")), token="p1"), feed(token="p1")]
    assert kinds(rig.items(push(), bid="1")) == ["file-changed"]
    first = google.requests[-1]
    assert first["path"] == "/drive/changes" and first["query"]["pageToken"] == "p0"
    assert rig.get_state(dp.STATE)["pageToken"] == "p1"
    assert rig.items(push(), bid="2") == []
    assert google.requests[-1]["query"]["pageToken"] == "p1", "the next batch resumes from the stored token"
    assert rig.get_state(dp.STATE)["pageToken"] == "p1"


def test_the_feed_is_read_across_pages(make, google):
    rig = make()
    seed(rig)
    google.drive_pages = [{"changes": [chg(fil("a"))], "nextPageToken": "pg2"},
                          {"changes": [chg(fil("b"))], "nextPageToken": "pg3"}, feed(chg(fil("c")), token="end")]
    out = rig.items(push())
    assert [i["file_id"] for i in out] == ["a", "b", "c"]
    assert [r["query"]["pageToken"] for r in google.requests if r["path"] == "/drive/changes"] == ["p0", "pg2", "pg3"]
    assert rig.get_state(dp.STATE)["pageToken"] == "end"


class ScriptedClient:
    """Answers ``client.get`` from a list, standing in for ``ctx.oauth(...)``."""

    def __init__(self, *pages):
        self.pages, self.params = list(pages), []

    def get(self, url, params=None, label=""):
        self.params.append(dict(params or {}))
        return self.pages.pop(0)


def test_a_read_cut_short_by_the_page_cap_stores_where_it_stopped():
    client = ScriptedClient({"changes": [chg(fil("a"))], "nextPageToken": "pg2"},
                            {"changes": [chg(fil("b"))], "nextPageToken": "pg3"})
    changes, token, finished = G.drive_changes(client, "p0", max_pages=2)
    assert [c["fileId"] for c in changes] == ["a", "b"] and token == "pg3" and finished is False
    assert [p["pageToken"] for p in client.params] == ["p0", "pg2"]


def test_a_read_to_the_end_stores_the_new_start_token():
    client = ScriptedClient({"changes": [], "newStartPageToken": "ns"})
    assert G.drive_changes(client, "p0") == ([], "ns", True)


@pytest.mark.parametrize("gone", [404, 410])
def test_an_expired_page_token_rebootstraps_quietly(make, google, gone):
    rig = make()
    seed(rig, "old")
    google.drive_pages = [gone]
    google.drive_start = "start-new"
    assert rig.items(push()) == []
    assert rig.get_state(dp.STATE)["pageToken"] == "start-new"
    assert google.requests[-1]["path"] == "/drive/changes/startPageToken"


def test_a_server_error_is_a_retry_and_leaves_the_state(make, google):
    rig = make()
    seed(rig)
    before = rig.get_state(dp.STATE)
    google.drive_pages = [500]
    rc, emits, err = rig.run(push())
    assert rc == 75 and emits == [] and rig.get_state(dp.STATE) == before
    assert "will be retried" in err and not os.path.exists(rig.state / "journal")


def test_a_rejected_request_is_permanent(make, google):
    rig = make()
    seed(rig)
    google.drive_pages = [403]
    rc, emits, err = rig.run(push())
    assert rc == 65 and emits == [] and "permanent failure" in err


def test_a_retry_after_a_crash_between_journal_and_state_loses_nothing(make, google, monkeypatch):
    rig = make()
    seed(rig, "p0")
    before = rig.get_state(dp.STATE)
    google.drive_pages = [feed(chg(fil("f1")), token="p1")]
    real = _bobbinrig.P._atomic_write
    state_path = rig.path(dp.STATE)

    def dying(path, obj):
        if path == state_path:
            raise OSError("killed while writing state")
        return real(path, obj)
    monkeypatch.setattr(_bobbinrig.P, "_atomic_write", dying)
    rc, emits, err = rig.run(push())
    assert rc == 75 and emits == [] and rig.get_state(dp.STATE) == before, "the token did not move"
    assert rig.journal() is not None
    monkeypatch.setattr(_bobbinrig.P, "_atomic_write", real)
    rc, emits, err = rig.run(push())
    assert rc == 0 and kinds([e["payload"] for e in emits]) == ["file-changed"]
    assert google.count("GET", "/drive/changes") == 1, "the retry did not read the feed again"
    assert rig.get_state(dp.STATE)["pageToken"] == "p1"
    rc, again, err = rig.run(push())                              # a third attempt: the lease was lost after the mark
    assert [e["payload"] for e in again] == [e["payload"] for e in emits] and google.count("GET", "/drive/changes") == 1


def test_a_real_push_wins_over_a_sync_ping_and_many_pushes_are_one_pass(make, google):
    rig = make()
    seed(rig)
    google.drive_pages = [feed(token="p9")]
    rig.items(push(n=1), push("sync", n=2), push(n=3))
    assert google.count("GET", "/drive/changes") == 1 and google.count("GET", "/drive/changes/startPageToken") == 0


def test_the_credential_is_never_rewritten_and_the_access_token_is_cached(make, google, tmp_path):
    rig = make()
    cred = tmp_path / "creds" / "drive.json"
    before = cred.read_text()
    seed(rig)
    google.drive_pages = [feed(token="p1"), feed(token="p1")]
    rig.items(push())
    rig.items(push(), bid="10")
    assert cred.read_text() == before and google.token_calls == 1
    assert all(r["auth"] == "Bearer tok-refreshed" for r in google.requests if r["path"].startswith("/drive"))


def test_a_missing_credential_is_permanent(make, google):
    rig = make()
    rig.env["HOOKSPOOL_SECRET_REFS"] = "{}"
    rc, emits, err = rig.run(push())
    assert rc == 65


# ---- filtering --------------------------------------------------------------------------------------------
def test_the_accounts_own_edits_are_dropped_whatever_the_case(make, google):
    rig = make(own=[OWNER.upper()])
    seed(rig)
    google.drive_pages = [feed(chg(fil("mine", by=OWNER)), chg(fil("theirs", by=OTHER)), chg(fil("noone", by="")))]
    assert [i["file_id"] for i in rig.items(push())] == ["theirs", "noone"]


def test_trashed_files_are_dropped_unless_asked_for(make, google):
    rig = make()
    seed(rig)
    google.drive_pages = [feed(chg(fil("gone", trashed=True)), chg(fil("kept")))]
    assert [i["file_id"] for i in rig.items(push())] == ["kept"]
    rig = make(include_trashed=True)
    seed(rig)
    google.drive_pages = [feed(chg(fil("gone", trashed=True)))]
    out = rig.items(push(), bid="2")
    assert [(i["file_id"], i["event_type"]) for i in out] == [("gone", "file-changed")]


def test_mime_types_are_skipped_exactly_or_by_family(make, google):
    rig = make(skip_mime_types=["application/vnd.google-apps.folder", "image/*"])
    seed(rig)
    google.drive_pages = [feed(chg(fil("d", mime="application/vnd.google-apps.folder")), chg(fil("p", mime="image/png")),
                               chg(fil("j", mime="IMAGE/jpeg")), chg(fil("t", mime="text/plain")),
                               chg(fil("g", mime="application/vnd.google-apps.document")))]
    assert [i["file_id"] for i in rig.items(push())] == ["t", "g"]


def test_removals_say_nothing_and_a_file_is_reported_once_for_its_last_change(make, google):
    rig = make()
    seed(rig)
    gone = {"fileId": "x", "removed": True, "time": AFTER}
    google.drive_pages = [feed(chg(fil("a", name="First")), gone, chg(fil("b")), chg(fil("a", name="Second"), LATER))]
    out = rig.items(push())
    assert [(i["file_id"], i["name"]) for i in out] == [("b", "Plan"), ("a", "Second")]


def test_the_high_water_follows_every_change_even_dropped_ones(make, google):
    rig = make()
    seed(rig)
    google.drive_pages = [feed(chg(fil("mine", by=OWNER), LATER), chg(fil("t", trashed=True), AFTER))]
    assert rig.items(push()) == []
    assert rig.get_state(dp.STATE)["high_water"] == "2026-10-01T12:00:00+00:00"
    google.drive_pages = [feed(chg(fil("old"), BEFORE), token="p3")]
    rig.items(push(), bid="2")
    assert rig.get_state(dp.STATE)["high_water"] == "2026-10-01T12:00:00+00:00", "it never goes back"


# ---- naming and fields ------------------------------------------------------------------------------------
def test_a_file_that_reached_the_feed_after_the_last_pass_is_created(make, google):
    rig = make()
    seed(rig)
    google.drive_pages = [feed(chg(fil("n", created=AFTER, modified=AFTER)), chg(fil("e", created=BEFORE)))]
    assert [(i["file_id"], i["event_type"]) for i in rig.items(push())] == [("n", "file-created"), ("e", "file-changed")]


def test_a_file_shared_with_the_account_is_shared_and_names_the_sharer(make, google):
    rig = make()
    seed(rig)
    f = fil("s", created=BEFORE, by="author@example.test", shared_at=AFTER, sharer=OTHER)
    both = fil("sn", created=AFTER, by="author@example.test", shared_at=AFTER, sharer=OTHER)
    old_share = fil("os", shared_at=BEFORE, sharer=OTHER, by="author@example.test")
    google.drive_pages = [feed(chg(f), chg(both), chg(old_share))]
    out = rig.items(push())
    assert [(i["file_id"], i["event_type"], i["actor"]) for i in out] == [
        ("s", "file-shared", OTHER), ("sn", "file-shared", OTHER), ("os", "file-changed", "author@example.test")]


def test_a_share_by_the_account_itself_is_dropped(make, google):
    rig = make()
    seed(rig)
    google.drive_pages = [feed(chg(fil("s", shared_at=AFTER, sharer=OWNER, by=OTHER)))]
    assert rig.items(push()) == []


def test_an_item_carries_the_file_facts_and_never_its_contents(make, google):
    rig = make()
    seed(rig)
    f = fil("f1", name="Quarterly plan", modified=LATER)
    f["description"], f["content"] = "must not travel", "must not travel"
    google.drive_pages = [feed(chg(f, LATER))]
    (item,) = rig.items(push())
    assert item == {"event_type": "file-changed", "source": "google-drive", "key": "file:f1", "file_id": "f1",
                    "name": "Quarterly plan", "mime_type": "application/vnd.google-apps.document",
                    "modified_time": LATER, "actor": OTHER, "web_link": "https://drive.example.test/file/f1",
                    "change_time": LATER}
    q = [r for r in google.requests if r["path"] == "/drive/changes"][-1]["query"]
    assert "content" not in q["fields"] and "description" not in q["fields"]


def test_a_shared_drive_is_asked_for_by_id_and_the_own_feed_is_not(make, google):
    rig = make(drive_id="0Axyz")
    google.drive_start = "s1"
    rig.items(push())
    starts = [r for r in google.requests if r["path"] == "/drive/changes/startPageToken"]
    assert starts[-1]["query"]["driveId"] == "0Axyz" and starts[-1]["query"]["supportsAllDrives"] == "true"
    rig.items(push(), bid="2")
    lst = [r for r in google.requests if r["path"] == "/drive/changes"][-1]["query"]
    assert lst["driveId"] == "0Axyz" and lst["includeItemsFromAllDrives"] == "true"
    plain = make()                                    # same state directory: the token is already there
    plain.items(push(), bid="3")
    assert "driveId" not in [r for r in google.requests if r["path"] == "/drive/changes"][-1]["query"]


def test_emits_follow_the_routes_and_an_unmapped_output_is_dropped(make, google):
    rig = make()
    seed(rig)
    google.drive_pages = [feed(chg(fil("a")), chg(fil("b", created=AFTER)))]
    rc, emits, err = rig.run(push(), routes={"file-created": "hermes:new"})
    assert rc == 0 and [(e["event_type"], e["action"], e["key"]) for e in emits] == [("file-created", "hermes:new", "file:b")]
    assert "dropped 1 item(s) of unmapped event types: file-changed x1" in err


def test_logs_carry_counts_never_names(make, google):
    rig = make()
    seed(rig)
    google.drive_pages = [feed(chg(fil("a", name="Secret Merger Plan")))]
    rc, emits, err = rig.run(push())
    assert rc == 0 and "Secret Merger Plan" not in err and OTHER not in err


# ---- the bobbin as a unit (14.3.1, 14.3.7, 14.3.8) --------------------------------------------------------
def package():
    pkgs, errs = PK.discover({})
    assert errs == [], errs
    return pkgs["google.drive"]


def test_the_manifest_is_valid_and_the_outputs_are_the_ones_the_code_produces():
    pkg = package()
    assert pkg.errors == [] and pkg.builtin and pkg.processor.get("state") is True
    assert set(dp.OUTPUTS) == set(pkg.outputs)
    assert pkg.secrets["cred"]["processor"] is True and "processor" not in pkg.secrets["channel_token"]
    for f in ("README.md", "SETUP.md", "AGENTS.md", "bobbin.toml", "source.py", "processor.py"):
        assert os.path.isfile(os.path.join(DIR, f)), f


def test_the_bobbin_calls_core_and_never_re_implements_it():
    assert R.violations(DIR) == []


def secret_files(tmp_path, *names):
    out = {}
    for n in names:
        p = tmp_path / f"{n}.secret"
        p.write_text("test-" + n + "\n")
        os.chmod(p, 0o600)
        out[n] = str(p)
    return out


def test_an_instance_expands_to_a_source_and_a_processor_destination(tmp_path):
    s = secret_files(tmp_path, "tok", "cred")
    p = tmp_path / "config.toml"
    p.write_text(f'''[spool]
home = "{tmp_path / "home"}"
[alerts]
dry = true
[bobbins.drive]
bobbin = "google.drive"
secrets.channel_token = {{ file = "{s['tok']}" }}
secrets.cred = {{ file = "{s['cred']}" }}
settings = {{ own = ["{OWNER}"], skip_mime_types = ["image/*"] }}
outputs = {{ file-shared = "hermes:shared", file-changed = "script:refresh" }}
expect = true
[destinations."script:refresh"]
argv = ["/bin/true"]
''')
    cfg = C.load_config(str(p))
    src, dst = cfg["sources"]["drive"], cfg["destinations"]["script:drive"]
    assert src["module"] == "hookspool.bobbins.google.drive.source" and src["action"] == "script:drive"
    assert src["span"] == "calendar" and src["expect_every_s"] == 604800
    assert dst["processor"] == "drive" and dst["max_inflight"] == 1
    env = dst["env"]
    assert json.loads(env["HOOKSPOOL_SECRET_REFS"]) == {"cred": {"file": s["cred"]}}
    settings = json.loads(env["HOOKSPOOL_SETTINGS"])
    assert settings["own"] == [OWNER] and settings["include_trashed"] is False and settings["api_url"].endswith("/drive/v3/")
    assert [e for e in C.check(cfg, check_secrets=True).errors if "drive" in e] == []


def read(doc):
    with open(os.path.join(DIR, doc), encoding="utf-8") as f:
        return f.read()


def test_every_secret_and_required_setting_appears_in_the_setup_documents():
    pkg = package()
    needed = list(pkg.secrets) + [k for k, v in pkg.settings.items() if v.get("required") is True]
    for doc in ("SETUP.md", "AGENTS.md"):
        for n in needed:
            assert n in read(doc), f"{doc} does not mention {n}"


def test_the_agent_document_has_the_needs_list_numbered_steps_and_marks_outward_steps():
    text = read("AGENTS.md")
    assert "## Needs from the human" in text
    steps = re.findall(r"^(\d+)\. \*\*", text, re.M)
    assert steps == [str(i) for i in range(1, len(steps) + 1)] and len(steps) >= 5
    assert text.count("`outward`") >= 3
    for placeholder in re.findall(r"<([^>\s]+)>", text):
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", placeholder), placeholder


@pytest.mark.parametrize("doc", ["SETUP.md", "AGENTS.md"])
def test_the_configuration_shown_in_the_document_expands(doc, tmp_path):
    blocks = [b for b in re.findall(r"```toml\n(.*?)```", read(doc), re.S) if "[bobbins.drive]" in b]
    assert len(blocks) == 1
    sd = tmp_path / "secrets"
    sd.mkdir()
    for fn in ("drive_channel_token", "owner.json"):
        (sd / fn).write_text("x")
        os.chmod(sd / fn, 0o600)
    block = (blocks[0].replace("<SECRETS_DIR>", str(sd)).replace("<CRED_FILE>", str(sd / "owner.json"))
             .replace("/etc/hookspool/secrets", str(sd)).replace("/etc/hookspool/google", str(sd))
             .replace("<OWNER_ADDRESS>", OWNER).replace("<DESTINATION>", "hermes:shared"))
    p = tmp_path / "config.toml"
    p.write_text(f'[spool]\nhome = "{tmp_path / "home"}"\n[alerts]\ndry = true\n' + block
                 + f'\n[hermes]\nsubscriptions_path = "{tmp_path / "subs.json"}"\n')
    cfg = C.load_config(str(p))
    assert "drive" in cfg["sources"]
