"""The ``google.contacts`` poll bobbin (ARCHITECTURE 14.3, 14.4): its poller through the processor runtime (journal,
staged state, emits) against a scripted People API (``tests/_peoplestub.py``):

* the sync-token loop: a silent first pass, incremental passes, deletions, an idle pass, paging;
* an expired token (HTTP 410, and a 400 that carried a token) -> one full read, ``full_resync``;
* replay of a retried batch, failures (exit 75 / 65) that change no state, the page cap;
* "Other contacts": a token of its own, switched on and off;
* no personal data: names, addresses and numbers the stub returns anyway never reach an emit, a log, the state or
  the journal, and the request asks for ``metadata`` only;
* the 14.3.7 rules, the poll expansion and the documents of 14.3.8.
"""
from __future__ import annotations

import json
import os
import re

import pytest

import _bobbinrig
import _bobbinrules as R
import _peoplestub
from sourcekit import SENTINEL

from hookspool import bobbin_loader as PK
from hookspool import config as C
from hookspool import net
from hookspool.providers import google as G

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DIR = os.path.join(ROOT, "hookspool", "bobbins", "google", "contacts")
OUTPUTS = ("contacts-changed",)
CONN = "/v1/people/me/connections"
OTHER = "/v1/otherContacts"


# ---- fixtures ---------------------------------------------------------------------------------------------
@pytest.fixture
def people():
    p = _peoplestub.PeopleStub()
    yield p
    p.close()


@pytest.fixture
def make(tmp_path, people):
    """``make(**settings)`` -> a Rig for the google.contacts bobbin."""
    def build_rig(**over):
        cred = _bobbinrig.write_cred(tmp_path / "creds" / "contacts.json", people.port)
        return _bobbinrig.Rig(tmp_path, "google.contacts", OUTPUTS, {**people.settings(), **over}, {"cred": cred})
    return build_rig


def tick(n: int = 1) -> dict:
    return {"tick": 1_800_000_000 + 900 * n, "instance": "contacts"}


def person(rid: str, deleted: bool = False, field: str = "people") -> dict:
    """A People API person as the stub returns it: more than the bobbin asks for, personal data included, to prove
    that none of it is read, kept or passed on."""
    p = {"resourceName": f"{field}/{rid}", "etag": "e-" + SENTINEL,
         "metadata": {"deleted": True} if deleted else {"sources": [{"type": "CONTACT", "id": rid}]}}
    if not deleted:
        p["names"] = [{"displayName": "Ada " + SENTINEL, "givenName": "Ada", "familyName": SENTINEL}]
        p["emailAddresses"] = [{"value": f"ada.{SENTINEL}@example.test"}]
        p["phoneNumbers"] = [{"value": "+1 555 0100 " + SENTINEL}]
    return p


def page(*people_, token=None, nxt=None, field="connections"):
    d: dict = {field: list(people_)}
    if nxt:
        d["nextPageToken"] = nxt
    if token:
        d["nextSyncToken"] = token
    return d


def seed(rig, token="sync-1", size=1000, other=None):
    state = {"connections": {"token": token, "page_size": size}}
    if other:
        state["other"] = {"token": other, "page_size": size}
    rig.seed_state("contacts_sync", state)


def state_of(rig):
    return rig.get_state("contacts_sync")


def items(emits):
    return [e["payload"] for e in emits]


# ---- the sync-token loop ----------------------------------------------------------------------------------
def test_the_first_pass_reads_the_list_stores_the_token_and_says_nothing(make, people):
    rig = make()
    people.connections = [page(person("c1"), person("c2"), token="sync-1")]
    rc, emits, err = rig.run(tick())
    assert (rc, emits) == (0, [])
    assert state_of(rig) == {"connections": {"token": "sync-1", "page_size": 1000}}
    [req] = people.calls(CONN)
    assert "syncToken" not in req["query"] and req["query"]["requestSyncToken"] == "true"
    assert req["query"]["pageSize"] == "1000" and req["auth"] == "Bearer tok-refreshed"
    assert people.calls(OTHER) == []


def test_the_request_asks_for_metadata_and_nothing_personal(make, people):
    rig = make()
    people.connections = [page(token="sync-1")]
    rig.run(tick())
    q = people.calls(CONN)[0]["query"]
    assert q["personFields"] == "metadata"
    assert q["fields"] == "connections(resourceName,metadata/deleted),nextPageToken,nextSyncToken"
    assert not {"names", "emailAddresses", "phoneNumbers"} & set(q["personFields"].split(","))


def test_a_later_pass_sends_the_stored_token_and_reports_changed_and_deleted(make, people):
    rig = make()
    seed(rig)
    people.connections = [page(person("c1"), person("c2", deleted=True), person("c3"), token="sync-2")]
    rc, emits, err = rig.run(tick(2))
    assert rc == 0 and len(emits) == 1
    assert items(emits) == [{"event_type": "contacts-changed", "changed": ["people/c1", "people/c3"],
                             "deleted": ["people/c2"], "full_resync": False, "truncated": False, "total_changed": 3}]
    assert emits[0]["action"] == "hermes:contacts-changed" and emits[0]["key"] == "k"
    assert people.calls(CONN)[0]["query"]["syncToken"] == "sync-1"
    assert state_of(rig)["connections"]["token"] == "sync-2"


def test_a_deletion_alone_is_reported(make, people):
    rig = make()
    seed(rig)
    people.connections = [page(person("c9", deleted=True), token="sync-2")]
    [item] = items(rig.run(tick())[1])
    assert item["changed"] == [] and item["deleted"] == ["people/c9"] and item["total_changed"] == 1


def test_an_idle_pass_emits_nothing_but_moves_the_token(make, people):
    rig = make()
    seed(rig)
    people.connections = [page(token="sync-2")]
    rc, emits, err = rig.run(tick())
    assert (rc, emits) == (0, []) and state_of(rig)["connections"]["token"] == "sync-2"


def test_a_contact_changed_then_deleted_in_one_read_is_only_deleted(make, people):
    rig = make()
    seed(rig)
    people.connections = [page(person("c1"), nxt="p2"), page(person("c1", deleted=True), token="sync-2")]
    [item] = items(rig.run(tick())[1])
    assert item["changed"] == [] and item["deleted"] == ["people/c1"]


def test_the_list_is_read_across_pages_with_the_token_on_every_page(make, people):
    rig = make()
    seed(rig)
    people.connections = [page(person("c1"), nxt="p2"), page(person("c2"), nxt="p3"),
                          page(person("c3"), token="sync-2")]
    [item] = items(rig.run(tick())[1])
    assert item["changed"] == ["people/c1", "people/c2", "people/c3"]
    qs = [r["query"] for r in people.calls(CONN)]
    assert [q.get("pageToken") for q in qs] == [None, "p2", "p3"]
    assert {q["syncToken"] for q in qs} == {"sync-1"}
    assert state_of(rig)["connections"]["token"] == "sync-2"


def test_names_that_are_not_resource_names_are_dropped_and_repeats_collapse(make, people):
    rig = make()
    seed(rig)
    odd = {"resourceName": "people/../../x"}
    people.connections = [page(person("c1"), odd, {"resourceName": 5}, {"x": 1}, person("c1"), "junk", token="sync-2")]
    [item] = items(rig.run(tick())[1])
    assert item["changed"] == ["people/c1"] and item["total_changed"] == 1


# ---- an expired token -------------------------------------------------------------------------------------
@pytest.mark.parametrize("status", [410, 400])
def test_an_expired_token_reads_everything_again_and_says_so(make, people, status):
    rig = make()
    seed(rig, size=500)
    people.connections = [status, page(person("c1"), person("c2"), nxt="p2"), page(person("c3"), token="sync-9")]
    rc, emits, err = rig.run(tick())
    assert rc == 0
    assert items(emits) == [{"event_type": "contacts-changed", "changed": ["people/c1", "people/c2", "people/c3"],
                             "deleted": [], "full_resync": True, "truncated": False, "total_changed": 3}]
    first, *rest = [r["query"] for r in people.calls(CONN)]
    assert first["syncToken"] == "sync-1" and first["pageSize"] == "500"
    assert all("syncToken" not in q for q in rest) and rest[0]["pageSize"] == "1000"
    assert state_of(rig) == {"connections": {"token": "sync-9", "page_size": 1000}}


def test_a_resync_of_an_empty_book_is_still_reported(make, people):
    rig = make()
    seed(rig)
    people.connections = [410, page(token="sync-9")]
    [item] = items(rig.run(tick())[1])
    assert item == {"event_type": "contacts-changed", "changed": [], "deleted": [], "full_resync": True, "truncated": False,
                    "total_changed": 0}


def test_a_400_without_a_token_is_not_taken_for_an_expired_one(make, people):
    rig = make()
    people.connections = [400]
    rc, emits, err = rig.run(tick())
    assert rc == 65 and emits == [] and not rig.has_state("contacts_sync")


def test_a_stored_page_size_is_kept_until_the_next_full_read(make, people):
    rig = make(page_size=200)
    seed(rig, size=1000)
    people.connections = [page(token="sync-2")]
    rig.run(tick())
    assert people.calls(CONN)[0]["query"]["pageSize"] == "1000"
    assert state_of(rig)["connections"]["page_size"] == 1000
    people.connections = [410, page(token="sync-3")]
    rig.run(tick(2), bid="10")
    assert people.calls(CONN)[-1]["query"]["pageSize"] == "200"
    assert state_of(rig)["connections"]["page_size"] == 200


@pytest.mark.parametrize("size", [0, 1001, -5])
def test_a_page_size_out_of_range_is_permanent(make, people, size):
    rig = make(page_size=size)
    rc, emits, err = rig.run(tick())
    assert rc == 65 and people.requests == []


# ---- replay and failures ----------------------------------------------------------------------------------
def test_a_retried_batch_replays_the_journal_and_asks_google_once(make, people):
    rig = make()
    seed(rig)
    people.connections = [page(person("c1"), token="sync-2"), page(person("c2"), token="sync-3")]
    rc1, first, _ = rig.run(tick(), bid="7")
    rc2, again, err = rig.run(tick(), bid="7")
    assert (rc1, rc2) == (0, 0) and again == first and len(first) == 1
    assert "replaying the journal" in err
    assert len(people.calls(CONN)) == 1 and state_of(rig)["connections"]["token"] == "sync-2"


@pytest.mark.parametrize("status,rc", [(500, 75), (503, 75), (429, 75), (403, 65), (404, 65)])
def test_failures_map_to_retry_or_permanent_and_leave_the_state(make, people, status, rc):
    rig = make()
    seed(rig)
    people.connections = [status]
    got, emits, err = rig.run(tick())
    assert got == rc and emits == [] and state_of(rig) == {"connections": {"token": "sync-1", "page_size": 1000}}


def test_the_first_pass_without_a_token_in_the_answer_is_retried(make, people):
    rig = make()
    people.connections = [page(person("c1"))]
    rc, emits, err = rig.run(tick())
    assert rc == 75 and not rig.has_state("contacts_sync")


def test_a_failure_on_the_second_list_discards_the_first_lists_progress(make, people):
    rig = make(include_other_contacts=True)
    seed(rig, other="other-1")
    people.connections = [page(person("c1"), token="sync-2")]
    people.other = [500]
    rc, emits, err = rig.run(tick())
    assert rc == 75 and emits == []
    assert state_of(rig)["connections"]["token"] == "sync-1"
    assert [r["query"]["syncToken"] for r in people.calls(CONN)] == ["sync-1"]
    people.connections = [page(person("c1"), token="sync-2")]
    people.other = [page(token="other-2", field="otherContacts")]
    rc, emits, err = rig.run(tick(2), bid="10")
    assert [r["query"]["syncToken"] for r in people.calls(CONN)] == ["sync-1", "sync-1"]
    assert rc == 0 and len(emits) == 1 and state_of(rig)["connections"]["token"] == "sync-2"


class FakeClient:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def get(self, url, params=None, label=None, **kw):
        self.calls.append(dict(params or {}))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def test_a_list_longer_than_the_page_cap_is_permanent():
    pages = [page(person(f"c{i}"), nxt=f"p{i}") for i in range(3)]
    with pytest.raises(net.Permanent):
        G.people_changes(FakeClient(*pages), "", max_pages=3)


def test_a_last_page_without_a_token_is_retryable():
    with pytest.raises(net.Retryable):
        G.people_changes(FakeClient(page(person("c1"))), "")


def test_an_error_page_with_no_status_is_not_taken_for_an_expired_token():
    with pytest.raises(net.Permanent):
        G.people_changes(FakeClient(net.Permanent("no status")), "sync-1")


# ---- Other contacts ---------------------------------------------------------------------------------------
def test_other_contacts_are_not_followed_by_default(make, people):
    rig = make()
    people.connections = [page(token="sync-1")]
    rig.run(tick())
    assert people.calls(OTHER) == []


def test_other_contacts_bootstrap_silently_with_a_token_of_their_own(make, people):
    rig = make(include_other_contacts=True)
    seed(rig)
    people.connections = [page(token="sync-2")]
    people.other = [page(person("c7", field="otherContacts"), token="other-1", field="otherContacts")]
    rc, emits, err = rig.run(tick())
    assert (rc, emits) == (0, [])
    assert state_of(rig) == {"connections": {"token": "sync-2", "page_size": 1000},
                             "other": {"token": "other-1", "page_size": 1000}}
    q = people.calls(OTHER)[0]["query"]
    assert q["readMask"] == "metadata" and "syncToken" not in q and "personFields" not in q
    assert q["fields"] == "otherContacts(resourceName,metadata/deleted),nextPageToken,nextSyncToken"


def test_each_list_reports_on_its_own(make, people):
    rig = make(include_other_contacts=True)
    seed(rig, other="other-1")
    people.connections = [page(person("c1"), token="sync-2")]
    people.other = [page(person("c8", field="otherContacts"), person("c9", deleted=True, field="otherContacts"),
                         token="other-2", field="otherContacts")]
    rc, emits, err = rig.run(tick())
    assert rc == 0
    assert items(emits) == [
        {"event_type": "contacts-changed", "changed": ["people/c1"], "deleted": [], "full_resync": False, "truncated": False,
         "total_changed": 1},
        {"event_type": "contacts-changed", "changed": ["otherContacts/c8"], "deleted": ["otherContacts/c9"],
         "full_resync": False, "truncated": False, "total_changed": 2}]
    assert people.calls(OTHER)[0]["query"]["syncToken"] == "other-1"
    assert people.calls(CONN)[0]["query"]["syncToken"] == "sync-1"


def test_an_expired_token_on_one_list_does_not_resync_the_other(make, people):
    rig = make(include_other_contacts=True)
    seed(rig, other="other-1")
    people.connections = [page(token="sync-2")]
    people.other = [410, page(person("c8", field="otherContacts"), token="other-9", field="otherContacts")]
    [item] = items(rig.run(tick())[1])
    assert item["full_resync"] is True and item["changed"] == ["otherContacts/c8"]
    assert [r["query"].get("syncToken") for r in people.calls(CONN)] == ["sync-1"]
    assert state_of(rig)["other"]["token"] == "other-9" and state_of(rig)["connections"]["token"] == "sync-2"


def test_switching_other_contacts_off_drops_their_token(make, people):
    rig = make()
    seed(rig, other="other-1")
    people.connections = [page(token="sync-2")]
    rig.run(tick())
    assert "other" not in state_of(rig) and people.calls(OTHER) == []


# ---- a bounded item --------------------------------------------------------------------------------------
def max_names() -> int:
    """The per-item limit, read from the poller file itself (a bobbin directory is not an importable package)."""
    ns: dict = {}
    with open(os.path.join(DIR, "poller.py"), encoding="utf-8") as fh:
        exec(compile(fh.read(), "poller.py", "exec"), ns)
    return ns["MAX_NAMES"]


def book(first: int, n: int, size: int = 1000, token="sync-9"):
    """Pages of n contacts named c<first>.., ``size`` to a page, the last one carrying the sync token."""
    out = []
    for i in range(0, n, size):
        chunk = [{"resourceName": f"people/c{first + j}", "metadata": {"sources": []}} for j in range(i, min(n, i + size))]
        out.append(page(*chunk, nxt=f"p{i + size}" if i + size < n else None, token=token if i + size >= n else None))
    return out


def test_a_full_resync_of_a_large_book_stays_under_the_stdout_cap(make, people):
    """About 33k names went over the processor's 1 MiB stdout cap and killed the tick on every retry."""
    rig = make()
    seed(rig)
    n = 60_000
    people.connections = [410] + book(1_000_000, n)
    rc, emits, err = rig.run(tick())
    assert rc == 0, err
    assert items(emits) == [{"event_type": "contacts-changed", "changed": [], "deleted": [], "full_resync": True,
                             "truncated": True, "total_changed": n}]
    assert len(json.dumps(emits)) < 1024
    assert state_of(rig)["connections"]["token"] == "sync-9"


def test_the_biggest_book_the_page_cap_reads_still_fits_in_two_items(make, people):
    rig = make(include_other_contacts=True)
    seed(rig, other="other-1")
    people.connections = [410] + book(1_000_000, G.PEOPLE_PAGES * 1000)
    people.other = [410] + [{("otherContacts" if k == "connections" else k): v for k, v in p.items()}
                            for p in book(2_000_000, G.PEOPLE_PAGES * 1000)]
    rc, emits, err = rig.run(tick())
    assert rc == 0, err
    assert [i["total_changed"] for i in items(emits)] == [100_000, 100_000] and all(i["truncated"] for i in items(emits))
    assert len(json.dumps(emits)) < 1024


def test_an_incremental_pass_lists_up_to_the_limit_and_summarises_beyond_it(make, people):
    cap = max_names()
    rig = make()
    seed(rig)
    people.connections = book(1, cap)
    [item] = items(rig.run(tick())[1])
    assert item["truncated"] is False and len(item["changed"]) == cap and item["total_changed"] == cap
    seed(rig)
    people.connections = book(1, cap + 1)
    [item] = items(rig.run(tick(2), bid="2")[1])
    assert item["truncated"] is True and item["changed"] == [] and item["deleted"] == []
    assert item["total_changed"] == cap + 1


def test_a_full_item_is_well_under_the_stdout_cap(make, people):
    """MAX_NAMES names at the longest name the parser accepts, two lists, must not reach 1 MiB."""
    long = "c" + "x" * 150
    rig = make(include_other_contacts=True)
    seed(rig, other="other-1")
    def pg(field, prefix):
        return page(*[{"resourceName": f"{prefix}/{long}{i:05d}", "metadata": {}} for i in range(max_names())],
                    token="t", field=field)

    people.connections, people.other = [pg("connections", "people")], [pg("otherContacts", "otherContacts")]
    rc, emits, err = rig.run(tick())
    assert rc == 0, err
    assert [len(i["changed"]) for i in items(emits)] == [max_names()] * 2
    assert 600_000 < len(json.dumps(emits)) < 800_000


# ---- no personal data -------------------------------------------------------------------------------------
def everything_written(rig) -> str:
    """Every byte the runtime left under the state directory: state files, the journal, the OAuth state."""
    out = []
    for root, _dirs, files in os.walk(rig.state):
        for f in files:
            with open(os.path.join(root, f), encoding="utf-8", errors="replace") as fh:
                out.append(fh.read())
    return "\n".join(out)


def test_personal_data_in_the_answers_never_reaches_an_emit_a_log_or_the_state(make, people):
    rig = make(include_other_contacts=True)
    seed(rig, other="other-1")
    people.connections = [page(person("c1"), person("c2", deleted=True), nxt="p2"),
                          page(person("c3"), token="sync-2")]
    people.other = [410, page(person("c8", field="otherContacts"), token="other-2", field="otherContacts")]
    rc, emits, err = rig.run(tick(), bid="5")
    assert rc == 0 and len(emits) == 2
    seen = json.dumps(emits) + err + everything_written(rig)
    for needle in (SENTINEL, "Ada", "example.test", "555", "displayName", "emailAddresses"):
        assert needle not in seen, needle
    assert set(items(emits)[0]) == {"event_type", "changed", "deleted", "full_resync", "truncated", "total_changed"}


def test_a_failed_pass_logs_no_resource_names_or_tokens(make, people):
    rig = make()
    seed(rig)
    people.connections = [page(person("c1"), nxt="p2"), 500]
    rc, emits, err = rig.run(tick())
    assert rc == 75
    for needle in (SENTINEL, "c1", "sync-1", "p2"):
        assert needle not in err, needle


def test_logs_carry_counts_only(make, people):
    rig = make()
    seed(rig)
    people.connections = [page(person("c1"), person("c2", deleted=True), token="sync-2")]
    rc, emits, err = rig.run(tick())
    assert "connections total=2" in err
    for needle in ("c1", "c2", "sync-1", "sync-2", "people/"):
        assert needle not in err, needle


def test_the_credential_is_never_rewritten_and_the_access_token_is_cached(make, people, tmp_path):
    rig = make()
    cred = tmp_path / "creds" / "contacts.json"
    before = cred.read_bytes()
    seed(rig)
    people.connections = [page(token="sync-2"), page(token="sync-3")]
    rig.run(tick(), bid="1")
    rig.run(tick(2), bid="2")
    assert cred.read_bytes() == before and people.token_calls == 1


def test_a_missing_credential_is_permanent(make, people):
    rig = make()
    rig.env["HOOKSPOOL_SECRET_REFS"] = "{}"
    rc, emits, err = rig.run(tick())
    assert rc == 65 and emits == [] and people.requests == []


def test_emits_follow_the_routes_and_an_unmapped_output_is_dropped(make, people):
    rig = make()
    seed(rig)
    people.connections = [page(person("c1"), token="sync-2")]
    rc, emits, err = rig.run(tick(), routes={})
    assert rc == 0 and emits == [] and "dropped 1 item(s) of unmapped event types: contacts-changed x1" in err


# ---- the bobbin as a unit (14.3.1, 14.3.7, 14.3.8, 14.4) ---------------------------------------------------
def package():
    pkgs, errs = PK.discover({})
    assert errs == [], errs
    return pkgs["google.contacts"]


def test_the_manifest_is_a_valid_poll_manifest_and_the_outputs_are_the_ones_the_code_produces():
    pkg = package()
    assert pkg.errors == [] and pkg.builtin and pkg.processor is None
    assert pkg.poll["every_s"] == 900 and pkg.poll["state"] is True and pkg.poll["module"] == "poller"
    assert set(OUTPUTS) == set(pkg.outputs)
    assert pkg.secrets["cred"]["processor"] is True
    prov = pkg.manifest["provider"]
    assert "webhook_docs" not in prov and "signature_docs" not in prov
    assert prov["no_push_note"] == "The People API has no watch method; incremental sync uses a sync token."
    assert prov["no_push_docs"] == "https://developers.google.com/people/api/rest"
    for f in ("README.md", "SETUP.md", "AGENTS.md", "bobbin.toml", "poller.py"):
        assert os.path.isfile(os.path.join(DIR, f)), f
    assert not os.path.exists(os.path.join(DIR, "source.py"))


def test_the_bobbin_calls_core_and_never_re_implements_it():
    assert R.violations(DIR) == []


def test_the_settings_have_the_documented_types_and_defaults():
    s = package().settings
    assert (s["page_size"]["type"], s["page_size"]["default"]) == ("int", 1000)
    assert (s["include_other_contacts"]["type"], s["include_other_contacts"]["default"]) == ("bool", False)
    assert s["api_url"]["default"] == G.PEOPLE_API


def secret_file(tmp_path):
    p = tmp_path / "cred.secret"
    p.write_text("test-cred\n")
    os.chmod(p, 0o600)
    return str(p)


def test_an_instance_expands_to_a_poll_source_and_a_processor_destination(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(f'''[spool]
home = "{tmp_path / "home"}"
[alerts]
dry = true
[bobbins.contacts]
bobbin = "google.contacts"
secrets.cred = {{ file = "{secret_file(tmp_path)}" }}
settings = {{ include_other_contacts = true, page_size = 500 }}
every_s = 600
outputs = {{ contacts-changed = "script:refresh" }}
[destinations."script:refresh"]
argv = ["/bin/true"]
''')
    cfg = C.load_config(str(p))
    src, dst = cfg["sources"]["contacts"], cfg["destinations"]["script:contacts"]
    assert src["kind"] == "poll" and src["every_s"] == 600 and "module" not in src
    assert src["action"] == "script:contacts" and src["span"] == "immediate"
    assert dst["processor"] == "contacts" and dst["max_inflight"] == 1
    settings = json.loads(dst["env"]["HOOKSPOOL_SETTINGS"])
    assert settings["include_other_contacts"] is True and settings["page_size"] == 500
    assert settings["api_url"] == G.PEOPLE_API
    assert json.loads(dst["env"]["HOOKSPOOL_SECRET_REFS"]) == {"cred": {"file": secret_file(tmp_path)}}
    assert [e for e in C.check(cfg, check_secrets=True).errors if "contacts" in e] == []


def read(doc):
    with open(os.path.join(DIR, doc), encoding="utf-8") as f:
        return f.read()


def test_every_secret_and_required_setting_appears_in_the_setup_documents():
    pkg = package()
    needed = list(pkg.secrets) + [k for k, v in pkg.settings.items() if v.get("required") is True]
    for doc in ("SETUP.md", "AGENTS.md"):
        for n in needed:
            assert n in read(doc), f"{doc} does not mention {n}"


def test_the_documents_open_with_the_poll_frontmatter():
    for doc in ("README.md", "SETUP.md", "AGENTS.md"):
        head = read(doc).split("---")[1]
        assert re.findall(r"^(\w+):", head, re.M) == ["bobbin", "provider", "api_docs"], doc
        assert "bobbin: google.contacts" in head and "provider: Google Contacts" in head


def test_the_agent_document_has_the_needs_list_and_numbered_steps():
    text = read("AGENTS.md")
    assert "## Needs from the human" in text
    steps = re.findall(r"^(\d+)\. \*\*", text, re.M)
    assert steps == [str(i) for i in range(1, len(steps) + 1)] and len(steps) >= 5
    for placeholder in re.findall(r"<([^>\s]+)>", text):
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", placeholder), placeholder


@pytest.mark.parametrize("doc", ["SETUP.md", "AGENTS.md"])
def test_the_configuration_shown_in_the_document_expands(doc, tmp_path):
    blocks = [b for b in re.findall(r"```toml\n(.*?)```", read(doc), re.S) if "[bobbins.contacts]" in b]
    assert len(blocks) == 1
    sd = tmp_path / "secrets"
    sd.mkdir()
    (sd / "owner.json").write_text("x")
    os.chmod(sd / "owner.json", 0o600)
    block = (blocks[0].replace("<CRED_FILE>", str(sd / "owner.json")).replace("/etc/hookspool/google", str(sd))
             .replace("<DESTINATION>", "hermes:contacts-changed"))
    p = tmp_path / "config.toml"
    p.write_text(f'[spool]\nhome = "{tmp_path / "home"}"\n[alerts]\ndry = true\n' + block
                 + f'\n[hermes]\nsubscriptions_path = "{tmp_path / "subs.json"}"\n')
    cfg = C.load_config(str(p))
    assert cfg["sources"]["contacts"]["kind"] == "poll"
