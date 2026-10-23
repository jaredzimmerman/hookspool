"""The ``gmail`` bobbin's processor (ARCHITECTURE 14.3): the behaviour of the Gmail delta script it replaces, run
through the processor runtime (journal, staged state, emits) against a scripted Google (``tests/_googlestub.py``).

* bootstrap, history reading, ``404`` on a stale history id, the handled list;
* the filters (settings, not another script's constants), scheduling detection, free windows;
* ``hooks.classify``: a private hand-off, and the contract around it;
* the outputs: ``scheduling``, ``agent-mail``, ``agent-schedule``, ``mailbox-changed``;
* failures: ``Retryable`` (exit 75) / ``Permanent`` (exit 65), and that a failed pass changes no state;
* the journal: a crash between the journal and the state write loses nothing, a stale replay moves nothing back;
* end to end through the real ``script`` destination and the dispatcher's fenced mark.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import _bobbinrig
import _googlestub
from hookspool.bobbins.gmail import processor as gm
from hookspool.destinations import build

OWNER = "owner@example.test"
AGENT = "agent@example.test"
TZ = "America/Los_Angeles"
OUTPUTS = ("scheduling", "agent-mail", "agent-schedule", "mailbox-changed")
AGENT_STATE = "gmail_push_state_agent"


# ---- fixtures ---------------------------------------------------------------------------------------------
@pytest.fixture
def google():
    g = _googlestub.GoogleStub()
    yield g
    g.close()


@pytest.fixture
def make(tmp_path, google):
    """``make(hooks=..., agent=True, **settings)`` -> a Rig for the gmail bobbin."""
    def build_rig(hooks=None, agent=True, creds=("owner", "agent"), **over):
        secrets = {f"cred_{n}": _bobbinrig.write_cred(tmp_path / "creds" / f"{n}.json", google.port) for n in creds}
        settings = {"owner": OWNER, "tz": TZ, "own_domains": ["ours.example.test"], **google.gmail_settings(), **over}
        if agent:
            settings.setdefault("agent", AGENT)
        return _bobbinrig.Rig(tmp_path, "gmail", OUTPUTS, settings, secrets, hooks=hooks)
    return build_rig


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def gmsg(mid, frm="Pat <pat@sender.example.test>", subject="Availability for a call?",
         text="Hi, are you available next week? What times work for you?", labels=("INBOX",), to=OWNER, cc=""):
    headers = [{"name": "From", "value": frm}, {"name": "To", "value": to}, {"name": "Subject", "value": subject},
               {"name": "Message-ID", "value": f"<{mid}@mail.example.test>"}]
    if cc:
        headers.append({"name": "Cc", "value": cc})
    return {"id": mid, "threadId": "t-" + mid, "labelIds": list(labels), "internalDate": str(int(time.time() * 1000)),
            "snippet": text[:40], "payload": {"mimeType": "multipart/alternative", "headers": headers,
                                              "parts": [{"mimeType": "text/plain", "body": {"data": b64url(text)}}]}}


def hist(*ids, history_id="120"):
    return {"history": [{"messagesAdded": [{"message": {"id": i}} for i in ids]}], "historyId": history_id}


def gpush(account=OWNER, history="110"):
    return {"emailAddress": account, "historyId": history}


def state_name(account):
    return gm.OWNER_STATE if account == OWNER else AGENT_STATE


def gstate(rig, account=OWNER):
    return rig.get_state(state_name(account))


def gseed(rig, account=OWNER, history="100", handled=()):
    rig.seed_state(state_name(account), {"historyId": history, "handled": list(handled)})


def hits(items):
    """The items that are not the ``mailbox-changed`` notices."""
    return [i for i in items if i["event_type"] != "mailbox-changed"]


def types(items):
    return [i["event_type"] for i in items]


MAPPED = {"scheduling": "hermes:scheduling", "agent-mail": "hermes:agent-mail", "agent-schedule": "hermes:agent-schedule"}


# ---- bootstrap and history -----------------------------------------------------------------------------------
def test_a_push_for_an_unwatched_mailbox_is_ignored(make, google):
    rig = make()
    assert rig.items(gpush("stranger@example.test")) == [] and google.requests == []
    rig = make(agent=False)                       # the assistant's mailbox is only watched when configured
    assert rig.items(gpush(AGENT), bid="2") == [] and google.requests == []


def test_first_push_bootstraps_the_history_id(make, google):
    rig = make()
    google.profile = {"historyId": "777"}
    assert rig.items(gpush()) == []
    st = gstate(rig)
    assert st["historyId"] == "777" and st["handled"] == [] and "bootstrapped" in st


def test_a_scheduling_request_gets_the_owners_free_windows(make, google):
    rig = make()
    gseed(rig)
    google.history = [hist("m1")]
    google.messages["m1"] = gmsg("m1")
    out = rig.items(gpush())
    assert types(out) == ["scheduling", "mailbox-changed"]
    h = out[0]
    assert (h["event_type"], h["account"], h["source"], h["message_id"], h["thread_id"]) == \
           ("scheduling", OWNER, "gmail", "m1", "t-m1")
    assert h["key"] == f"{OWNER}:t-m1"
    assert h["from_address"] == "pat@sender.example.test" and h["from_domain"] == "sender.example.test"
    assert h["subject"] == "Availability for a call?" and h["rfc_message_id"] == "<m1@mail.example.test>"
    assert h["excerpt"].startswith("Hi, are you available") and "other_hits" not in h
    assert h["free_windows_text"].startswith("- ") or "no free working-hours window" in h["free_windows_text"]
    st = gstate(rig)
    assert st["historyId"] == "120" and st["handled"] == ["m1"]
    body = json.loads([r for r in google.requests if r["path"] == "/freebusy"][0]["body"])
    assert body["timeZone"] == TZ and body["items"] == [{"id": "primary"}]
    # the history read asks for added inbox messages
    q = [r for r in google.requests if r["path"] == "/gmail/history"][0]["query"]
    assert q["startHistoryId"] == "100" and q["labelId"] == "INBOX" and q["historyTypes"] == "messageAdded"


def test_a_message_that_is_not_a_scheduling_request_is_silent_but_advances_the_history(make, google):
    rig = make()
    gseed(rig)
    google.history = [hist("m2")]
    google.messages["m2"] = gmsg("m2", subject="Hello", text="Just saying hi.")
    assert types(rig.items(gpush())) == ["mailbox-changed"]
    assert gstate(rig)["historyId"] == "120" and gstate(rig)["handled"] == ["m2"]
    assert google.count("POST", "/freebusy") == 0, "no request, no free/busy read"


def test_already_handled_ids_are_not_fetched_again(make, google):
    rig = make()
    gseed(rig, handled=["m1"])
    google.history = [hist("m1", "m3")]
    google.messages["m3"] = gmsg("m3", subject="Hello", text="hi")
    rig.items(gpush())
    fetched = [r["path"] for r in google.requests if r["path"].startswith("/gmail/messages/")]
    assert fetched == ["/gmail/messages/m3"]
    assert gstate(rig)["handled"] == ["m1", "m3"]


def test_the_handled_list_is_capped(make, google):
    rig = make()
    gseed(rig, handled=[f"old{i}" for i in range(299)])
    google.history = [hist("n1", "n2", "n3")]
    for i in ("n1", "n2", "n3"):
        google.messages[i] = gmsg(i, subject="Hello", text="hi")
    rig.items(gpush())
    h = gstate(rig)["handled"]
    assert len(h) == 300 and h[-3:] == ["n1", "n2", "n3"] and h[0] == "old2"


def test_a_message_deleted_after_its_history_entry_is_skipped(make, google):
    rig = make()
    gseed(rig)
    google.history = [hist("gone1")]
    assert rig.items(gpush()) == []
    assert gstate(rig)["historyId"] == "120"


def test_history_is_read_across_pages(make, google):
    rig = make()
    gseed(rig)
    google.history = [dict(hist("a"), nextPageToken="p2", historyId="115"), hist("b", history_id="125")]
    for i in ("a", "b"):
        google.messages[i] = gmsg(i, subject="Hello", text="hi")
    out = rig.items(gpush())
    assert out[0]["new_messages"] == 2
    assert gstate(rig)["historyId"] == "125" and gstate(rig)["handled"] == ["a", "b"]


def test_a_history_gone_rebootstraps_and_other_errors_fail(make, google):
    rig = make()
    gseed(rig)
    google.history = [404]
    google.profile = {"historyId": "900"}
    assert rig.items(gpush()) == [] and gstate(rig)["historyId"] == "900"
    google.history = [500]
    rc, emits, err = rig.run(gpush(), bid="2")
    assert rc == 75 and emits == []
    assert gstate(rig)["historyId"] == "900"
    google.history = [403]
    rc, emits, err = rig.run(gpush(), bid="3")
    assert rc == 65 and "permanent failure" in err and emits == []


# ---- the filters ---------------------------------------------------------------------------------------------
FILTERS = dict(skip_domains=["skipme.example.test"], skip_keywords=["newsletter", "sale"])


@pytest.mark.parametrize("label,msg", [
    ("promotional", dict(labels=("INBOX", "CATEGORY_PROMOTIONS"))),
    ("not inbox", dict(labels=("SENT",))),
    ("from the owner", dict(frm=OWNER)),
    ("from the agent", dict(frm=f"Agent <{AGENT}>")),
    ("from an own domain", dict(frm="Bot <bot@ours.example.test>")),
    ("no-reply", dict(frm="noreply@sender.example.test")),
    ("skip domain", dict(frm="x@skipme.example.test")),
    ("a subdomain of a skip domain", dict(frm="x@mail.skipme.example.test")),
    ("skip keyword", dict(subject="Big sale on availability")),
])
def test_filtered_mail_never_reaches_the_agent(make, google, label, msg):
    rig = make(**FILTERS)
    gseed(rig)
    google.history = [hist("m9")]
    google.messages["m9"] = gmsg("m9", **msg)
    assert hits(rig.items(gpush())) == [], label


def test_whole_word_keywords_and_the_recruiting_override(make, google):
    rig = make(**FILTERS)
    gseed(rig)
    google.history = [hist("a", "b")]
    google.messages["a"] = gmsg("a", subject="Salesforce availability", text="What times work for a call?")
    google.messages["b"] = gmsg("b", subject="Interview sale availability", text="What times work?")
    out = hits(rig.items(gpush()))
    assert [h["message_id"] for h in out] == ["a", "b"], "'Salesforce' is not 'sale'; the recruiting word overrides 'sale'"
    assert all(h["free_windows_text"] == out[0]["free_windows_text"] for h in out)
    assert google.count("POST", "/freebusy") == 1, "one free/busy read serves every request in the pass"


def test_an_exact_address_in_skip_domains_skips_that_sender_only(make, google):
    rig = make(skip_domains=["bot@sender.example.test"])
    gseed(rig)
    google.history = [hist("a", "b")]
    google.messages["a"] = gmsg("a", frm="Bot <bot@sender.example.test>")
    google.messages["b"] = gmsg("b", frm="Pat <pat@sender.example.test>")
    assert [h["message_id"] for h in hits(rig.items(gpush()))] == ["b"]


def test_important_senders_are_never_skipped_by_the_filters_but_never_count_as_us(make, google):
    rig = make(skip_domains=["sender.example.test"], skip_keywords=["availability"],
               important_senders=["pat@sender.example.test", "@vip.example.test"])
    gseed(rig)
    google.history = [hist("a", "b", "c", "d")]
    google.messages["a"] = gmsg("a", frm="Pat <pat@sender.example.test>")
    google.messages["b"] = gmsg("b", frm="Sam <sam@sender.example.test>")
    google.messages["c"] = gmsg("c", frm="noreply@vip.example.test")
    google.messages["d"] = gmsg("d", frm="Bot <bot@ours.example.test>")
    assert [h["message_id"] for h in hits(rig.items(gpush()))] == ["a", "c"]


def test_mail_with_the_agent_on_the_thread_gets_no_draft_request(make, google):
    rig = make()
    gseed(rig)
    google.history = [hist("m4")]
    google.messages["m4"] = gmsg("m4", cc=AGENT)
    assert hits(rig.items(gpush())) == []
    rig = make(agent=False)                       # with no assistant mailbox, an address in Cc means nothing
    gseed(rig)
    google.history = [hist("m4")]
    assert [h["message_id"] for h in hits(rig.items(gpush(), bid="2"))] == ["m4"]


def test_invalid_working_hours_fall_back_to_the_default(make):
    assert gm._hours("nonsense") == (9, 0, 17, 30) and gm._hours("8:15-16:45") == (8, 15, 16, 45)


# ---- free windows --------------------------------------------------------------------------------------------
class FakeClient:
    def __init__(self, busy):
        self.busy, self.sent = busy, []

    def post(self, url, json=None, label=None, **kw):
        self.sent.append((url, json))
        return {"calendars": {"primary": {"busy": self.busy}}}


def test_free_windows_are_the_gaps_in_working_hours_and_skip_weekends():
    cfg = SimpleNamespace(tz=ZoneInfo("UTC"), tz_name="UTC", hours=(9, 0, 17, 30), freebusy_url="https://fb.example.test/x")
    now = datetime(2026, 10, 30, 6, 0, tzinfo=ZoneInfo("UTC"))                  # a Friday
    busy = [{"start": "2026-10-30T10:00:00+00:00", "end": "2026-10-30T11:00:00+00:00"},
            {"start": "2026-10-30T13:00:00+00:00", "end": "2026-10-30T13:20:00+00:00"},
            {"start": "2026-11-02T09:00:00+00:00", "end": "2026-11-02T17:30:00+00:00"}]
    client = FakeClient(busy)
    got = gm.free_windows(client, cfg, now, days=3)
    assert got == ["Fri Oct 30, 9 AM–10 AM", "Fri Oct 30, 11 AM–1 PM", "Fri Oct 30, 1:20 PM–5:30 PM"], \
        "a 20 minute gap is too short, the weekend is skipped, and the busy Monday has no window"
    url, body = client.sent[0]
    assert url == "https://fb.example.test/x" and body["timeZone"] == "UTC" and body["items"] == [{"id": "primary"}]


def test_free_windows_start_two_hours_from_now_and_are_capped():
    cfg = SimpleNamespace(tz=ZoneInfo("UTC"), tz_name="UTC", hours=(9, 0, 17, 30), freebusy_url="u")
    now = datetime(2026, 10, 28, 12, 0, tzinfo=ZoneInfo("UTC"))                 # a Wednesday
    got = gm.free_windows(FakeClient([]), cfg, now, days=6, cap=3)
    assert got == ["Wed Oct 28, 2 PM–5:30 PM", "Thu Oct 29, 9 AM–5:30 PM", "Fri Oct 30, 9 AM–5:30 PM"]


# ---- the assistant's mailbox ---------------------------------------------------------------------------------
def test_the_agents_own_mailbox_reports_its_mail(make, google):
    rig = make()
    gseed(rig, AGENT)
    google.history = [hist("a1", "a2")]
    google.messages["a1"] = gmsg("a1", to=AGENT, subject="Receipt", text="thanks")
    google.messages["a2"] = gmsg("a2", to=AGENT, subject="Invoice", text="attached")
    out = rig.items(gpush(AGENT))
    assert types(hits(out)) == ["agent-mail", "agent-mail"] and out[0]["account"] == AGENT
    assert "free_windows_text" not in out[0] and gstate(rig, AGENT)["handled"] == ["a1", "a2"]
    assert google.count("POST", "/freebusy") == 0
    assert all(r["auth"] == "Bearer tok-refreshed" for r in google.requests if r["path"].startswith("/gmail"))


def test_every_scheduling_request_in_one_pass_is_reported(make, google):
    """A coalesced batch can hold several requests; the first must not hide the rest."""
    rig = make()
    gseed(rig)
    google.history = [hist("m1", "m2", history_id="130")]
    google.messages["m1"] = gmsg("m1")
    google.messages["m2"] = gmsg("m2", frm="Sam <sam@other.example.test>", subject="Times?", text="When are you free this week?")
    out = hits(rig.items(gpush()))
    assert [h["message_id"] for h in out] == ["m1", "m2"] and [h["thread_id"] for h in out] == ["t-m1", "t-m2"]
    assert gstate(rig)["handled"] == ["m1", "m2"], "both are handled because both were reported"


def test_the_agents_mailbox_reports_a_burst_of_any_size(make, google):
    rig = make()
    gseed(rig, AGENT)
    ids = [f"b{i}" for i in range(9)]
    google.history = [hist(*ids, history_id="130")]
    for i in ids:
        google.messages[i] = gmsg(i, to=AGENT, subject=f"Note {i}", text="fyi")
    out = hits(rig.items(gpush(AGENT)))
    assert [h["message_id"] for h in out] == ids and gstate(rig, AGENT)["handled"] == ids


def test_a_custom_agent_state_name_keeps_an_existing_state_file(make, google):
    rig = make(agent_state="gmail_push_state_assistant.json")
    rig.seed_state("gmail_push_state_assistant", {"historyId": "100", "handled": []})
    google.history = [hist("a1")]
    google.messages["a1"] = gmsg("a1", to=AGENT, subject="Receipt", text="thanks")
    assert types(hits(rig.items(gpush(AGENT)))) == ["agent-mail"]
    assert rig.get_state("gmail_push_state_assistant")["historyId"] == "120" and not rig.has_state(AGENT_STATE)


def test_the_agent_credential_is_optional_until_an_agent_mailbox_is_configured(make, google):
    rig = make(agent=False, creds=("owner",))
    gseed(rig)
    google.history = [hist("m1")]
    google.messages["m1"] = gmsg("m1")
    assert types(hits(rig.items(gpush()))) == ["scheduling"]
    rig = make(creds=("owner",))                   # an assistant address without its credential: a person must fix it
    gseed(rig, AGENT)
    rc, emits, err = rig.run(gpush(AGENT), bid="2")
    assert rc == 65 and emits == []


# ---- mailbox-changed -----------------------------------------------------------------------------------------
def test_mailbox_changed_is_one_per_mailbox_per_batch_and_only_when_new_mail_was_read(make, google):
    rig = make()
    gseed(rig)
    gseed(rig, AGENT)
    google.history = [hist("m1", "m2", history_id="131"), hist("a1", history_id="132")]
    google.messages["m1"] = gmsg("m1", subject="Hello", text="hi")
    google.messages["m2"] = gmsg("m2", subject="Hello again", text="hi")
    google.messages["a1"] = gmsg("a1", to=AGENT, subject="Receipt", text="thanks")
    out = rig.items(gpush(OWNER), gpush(OWNER), gpush(AGENT))
    notes = [i for i in out if i["event_type"] == "mailbox-changed"]
    assert [(n["account"], n["new_messages"], n["history_id"], n["key"]) for n in notes] == \
           [(OWNER, 2, "131", OWNER), (AGENT, 1, "132", AGENT)]
    assert all(n["source"] == "gmail" for n in notes)
    # nothing new: nothing to wake
    google.history = [hist(history_id="140")]
    assert rig.items(gpush(OWNER), bid="2") == []


def test_mailbox_changed_follows_the_mailbox_items_and_is_dropped_when_unmapped(make, google):
    rig = make()
    gseed(rig)
    google.history = [hist("m1")]
    google.messages["m1"] = gmsg("m1")
    rc, emits, err = rig.run(gpush(), routes={"scheduling": "hermes:mail"})
    assert rc == 0 and [e["event_type"] for e in emits] == ["scheduling"]
    assert "mailbox-changed x1" in err
    assert (emits[0]["dedup_id"], emits[0]["action"], emits[0]["route"]) == ("hs_test_9:0", "hermes:mail", "mail")


# ---- hooks.classify --------------------------------------------------------------------------------------------
SCHED_HOOK = """
seen = []

def classify(message, account, ctx):
    seen.append((message, account))
    if "[sched]" in message["headers"].get("subject", ""):
        return {"event_type": "agent-schedule", "kind": "reply", "tracked_thread_id": "tracked-1",
                "meeting_subject": "Tracked meeting", "ref_tag": "[ref-tracked-1]",
                "new_text": message["body"][:3000]}
    return None
"""


def test_without_a_hook_the_message_takes_the_generic_path(make, google):
    rig = make()
    gseed(rig, AGENT)
    google.history = [hist("s1")]
    google.messages["s1"] = gmsg("s1", to=AGENT, subject="Re: meeting [sched]", text="Tuesday works")
    assert types(hits(rig.items(gpush(AGENT)))) == ["agent-mail"]


def test_a_hook_hands_a_message_off_privately_and_the_bobbin_adds_the_standard_fields(make, google):
    rig = make(hooks=SCHED_HOOK)
    gseed(rig, AGENT)
    google.history = [hist("s1", "a2")]
    google.messages["s1"] = gmsg("s1", to=AGENT, subject="Re: meeting [sched]", text="Tuesday works")
    google.messages["a2"] = gmsg("a2", to=AGENT, subject="Receipt", text="thanks")
    out = hits(rig.items(gpush(AGENT)))
    assert types(out) == ["agent-schedule", "agent-mail"], "hand-offs lead the pass"
    s = out[0]
    assert s["kind"] == "reply" and s["tracked_thread_id"] == "tracked-1" and s["meeting_subject"] == "Tracked meeting"
    assert s["ref_tag"] == "[ref-tracked-1]" and s["new_text"] == "Tuesday works"
    assert (s["account"], s["source"], s["message_id"], s["thread_id"], s["rfc_message_id"]) == \
           (AGENT, "gmail", "s1", "t-s1", "<s1@mail.example.test>")
    assert s["key"] == f"{AGENT}:tracked-1", "one Hermes event per tracked thread"
    assert s["free_windows_text"].startswith("- ") or "no free" in s["free_windows_text"]
    assert google.count("POST", "/freebusy") == 1
    body = json.loads([r for r in google.requests if r["path"] == "/freebusy"][0]["body"])
    assert body["timeMax"] > body["timeMin"]
    assert gstate(rig, AGENT)["handled"] == ["s1", "a2"]


def test_the_hook_gets_the_parsed_message_and_the_account(make, google):
    rig = make(hooks=SCHED_HOOK)
    gseed(rig)
    google.history = [hist("m1")]
    google.messages["m1"] = gmsg("m1", cc="cc@example.test")
    rig.items(gpush())
    (message, account), = sys.modules["hookspool._hooks_gmail"].seen
    assert account == OWNER
    assert sorted(message) == ["body", "headers", "id", "internal_date", "labels", "snippet", "thread_id"]
    assert (message["id"], message["thread_id"], message["labels"]) == ("m1", "t-m1", ["INBOX"])
    assert message["headers"]["subject"] == "Availability for a call?" and message["headers"]["cc"] == "cc@example.test"
    assert message["headers"]["from"] == "Pat <pat@sender.example.test>" and message["headers"]["to"] == OWNER
    assert message["body"].startswith("Hi, are you available") and isinstance(message["internal_date"], int)


def test_a_hook_item_may_set_its_own_standard_fields(make, google):
    hook = """
def classify(message, account, ctx):
    return {"event_type": "agent-schedule", "key": "custom", "thread_id": "own-thread", "free_windows_text": "- mine"}
"""
    rig = make(hooks=hook)
    gseed(rig, AGENT)
    google.history = [hist("s1")]
    google.messages["s1"] = gmsg("s1", to=AGENT)
    s = hits(rig.items(gpush(AGENT)))[0]
    assert (s["key"], s["thread_id"], s["free_windows_text"], s["message_id"]) == ("custom", "own-thread", "- mine", "s1")


@pytest.mark.parametrize("bad", ['"agent-schedule"', '{"event_type": "nonsense"}', '{"kind": "reply"}', '["x"]'])
def test_a_hook_item_that_is_not_a_declared_output_is_permanent_and_moves_nothing(make, google, bad):
    rig = make(hooks=f"def classify(message, account, ctx):\n    return {bad}\n")
    gseed(rig, AGENT)
    google.history = [hist("s1")]
    google.messages["s1"] = gmsg("s1", to=AGENT)
    rc, emits, err = rig.run(gpush(AGENT))
    assert rc == 65 and emits == [] and "event_type is not a declared output" in err
    assert gstate(rig, AGENT) == {"historyId": "100", "handled": []}


def test_a_hook_that_raises_retries_the_batch_and_moves_nothing(make, google):
    rig = make(hooks="def classify(message, account, ctx):\n    raise ValueError('boom')\n")
    gseed(rig)
    google.history = [hist("m1")]
    google.messages["m1"] = gmsg("m1")
    rc, emits, err = rig.run(gpush())
    assert rc == 75 and emits == [] and "boom" not in err
    assert gstate(rig)["historyId"] == "100"


def test_a_hooks_module_without_classify_is_harmless(make, google):
    rig = make(hooks="def travel(event, day_events, ctx):\n    return None\n")
    gseed(rig)
    google.history = [hist("m1")]
    google.messages["m1"] = gmsg("m1")
    assert types(hits(rig.items(gpush()))) == ["scheduling"]


def test_a_hooks_file_that_is_group_writable_is_refused(make, google, tmp_path):
    rig = make(hooks="def classify(message, account, ctx):\n    return None\n")
    os.chmod(tmp_path / "hooks.py", 0o666)
    rc, emits, err = rig.run(gpush())
    assert rc == 75 and emits == []


# ---- batches -----------------------------------------------------------------------------------------------------
def test_pushes_for_a_mailbox_are_one_history_read_and_results_are_keyed_by_thread(make, google):
    rig = make()
    gseed(rig)
    google.history = [hist("m1", history_id="130")]
    google.messages["m1"] = gmsg("m1")
    rc, emits, err = rig.run(gpush(history="111"), gpush(history="112"), gpush(history="113"),
                             routes={"scheduling": "hermes:mail"})
    assert rc == 0 and google.count("GET", "/gmail/history") == 1, "three pushes, one history read"
    assert len(emits) == 1
    e = emits[0]
    assert (e["source"], e["key"], e["action"], e["route"], e["event_type"]) == \
           ("gmail", f"{OWNER}:t-m1", "hermes:mail", "mail", "scheduling")
    assert e["dedup_id"] == "hs_test_9:0" and e["payload"]["message_id"] == "m1" and (e["quiet_s"], e["max_s"]) == (0, 0)
    assert gstate(rig)["historyId"] == "130"


def test_every_request_of_a_coalesced_batch_is_emitted(make, google):
    rig = make()
    gseed(rig)
    google.history = [hist("m1", "m2", history_id="130")]
    google.messages["m1"] = gmsg("m1")
    google.messages["m2"] = gmsg("m2", frm="Sam <sam@other.example.test>", subject="Times?", text="When are you free?")
    rc, emits, err = rig.run(gpush(history="111"), gpush(history="112"), routes=MAPPED)
    assert rc == 0 and [e["payload"]["message_id"] for e in emits] == ["m1", "m2"]
    assert [e["key"] for e in emits] == [f"{OWNER}:t-m1", f"{OWNER}:t-m2"], "each thread is its own Hermes event"
    assert [e["dedup_id"] for e in emits] == ["hs_test_9:0", "hs_test_9:1"]
    assert gstate(rig)["handled"] == ["m1", "m2"]


def test_two_mailboxes_in_one_batch(make, google):
    rig = make()
    gseed(rig)
    gseed(rig, AGENT)
    google.history = [hist("m1", history_id="131"), hist("a1", history_id="132")]
    google.messages["m1"] = gmsg("m1")
    google.messages["a1"] = gmsg("a1", to=AGENT, subject="Receipt", text="thanks")
    rc, emits, err = rig.run(gpush(OWNER), gpush(AGENT), routes=MAPPED)
    assert rc == 0 and sorted(e["event_type"] for e in emits) == ["agent-mail", "scheduling"]
    assert sorted(e["dedup_id"] for e in emits) == ["hs_test_9:0", "hs_test_9:2"], "ids follow the item index"


def test_a_failed_pass_changes_nothing(make, google):
    rig = make()
    gseed(rig)
    google.history = [500]
    rc, emits, err = rig.run(gpush())
    assert rc == 75 and emits == [] and gstate(rig)["historyId"] == "100"
    assert not os.path.exists(rig.state / "journal"), "a failed pass leaves no journal"


def test_the_state_of_one_mailbox_is_not_lost_when_the_other_fails(make, google):
    rig = make()
    gseed(rig)
    gseed(rig, AGENT)
    google.history = [hist("m1", history_id="131"), 500]
    google.messages["m1"] = gmsg("m1")
    rc, emits, err = rig.run(gpush(OWNER), gpush(AGENT))
    assert rc == 75 and gstate(rig)["historyId"] == "100" and gstate(rig, AGENT)["historyId"] == "100"


# ---- the journal ----------------------------------------------------------------------------------------------------
def failing_state_writes(monkeypatch, rig, only_for=None):
    real = _bobbinrig.P._atomic_write
    state_dir = str(rig.state)

    def dying(path, obj):
        if os.path.dirname(path) == state_dir and os.path.basename(path) != ".runtime.json" \
                and (only_for is None or path == only_for):
            raise OSError("killed while writing state")
        return real(path, obj)
    monkeypatch.setattr(_bobbinrig.P, "_atomic_write", dying)
    return real


def test_a_retry_after_a_crash_replays_the_result_and_advances_once(make, google, monkeypatch):
    rig = make()
    gseed(rig)
    google.history = [hist("m1", history_id="140")]
    google.messages["m1"] = gmsg("m1")
    real = failing_state_writes(monkeypatch, rig, only_for=rig.path(gm.OWNER_STATE))
    rc, emits, err = rig.run(gpush(), routes=MAPPED)
    assert rc == 75 and emits == [] and gstate(rig)["historyId"] == "100"
    monkeypatch.setattr(_bobbinrig.P, "_atomic_write", real)
    rc, emits, err = rig.run(gpush(), routes=MAPPED)
    assert rc == 0 and len(emits) == 1 and gstate(rig)["historyId"] == "140"
    assert google.count("GET", "/gmail/history") == 1, "the retry replayed the journal"


def test_replaying_an_old_unapplied_journal_does_not_move_the_state_back(make, google, monkeypatch):
    """The state write fails until the batch goes dead; a later batch is delivered; an operator then replays the old
    one. Its older history id and handled list must not overwrite the newer state."""
    rig = make()
    gseed(rig)
    google.history = [hist("m1", history_id="140")]
    google.messages["m1"] = gmsg("m1")
    real = failing_state_writes(monkeypatch, rig)
    rc, emits, err = rig.run(gpush(), bid="old", routes=MAPPED)
    assert rc == 75
    monkeypatch.setattr(_bobbinrig.P, "_atomic_write", real)
    google.history = [hist("m5", history_id="150")]
    google.messages["m5"] = gmsg("m5", subject="Hello", text="hi")
    rc, emits, err = rig.run(gpush(), bid="newer", routes=MAPPED)
    assert rc == 0 and emits == [] and gstate(rig)["historyId"] == "150" and gstate(rig)["handled"] == ["m5"]
    rc, emits, err = rig.run(gpush(), bid="old", routes=MAPPED)
    assert rc == 0 and [e["payload"]["message_id"] for e in emits] == ["m1"], "the old result is still replayed"
    assert gstate(rig)["historyId"] == "150" and gstate(rig)["handled"] == ["m5"], "the newer state is untouched"
    assert "not applying stale state" in err


def test_a_journal_replay_after_a_partial_apply_does_not_rewrite_what_was_written(make, google, monkeypatch):
    rig = make()
    gseed(rig)
    google.history = [hist("m1", history_id="140")]
    google.messages["m1"] = gmsg("m1")
    calls = []
    real = _bobbinrig.P._atomic_write

    def counting(path, obj):
        if path == rig.path(gm.OWNER_STATE):
            calls.append(path)
        return real(path, obj)
    monkeypatch.setattr(_bobbinrig.P, "_atomic_write", counting)
    assert rig.run(gpush(), bid="x")[0] == 0 and len(calls) == 1
    j = rig.journal("x")
    j["applied"] = False                                       # as if killed after the write, before the flag
    real(_bobbinrig.P._journal_path(str(rig.state), "x"), j)
    assert rig.run(gpush(), bid="x")[0] == 0 and len(calls) == 1, "already written: not written twice"


# ---- end to end: the real script destination and the dispatcher ---------------------------------------------------
def events_for(r, source):
    return [dict(x) for x in r.store._conn().execute(
        "SELECT e.id, e.event_type, e.dedup_id, b.key, b.destination, b.route FROM events e JOIN batches b "
        "ON b.id = e.batch_id WHERE b.source=? AND b.destination LIKE 'hermes:%' ORDER BY e.id", (source,))]


def processor_destination(rig, tmp_path, name="script:mail"):
    envfile = tmp_path / "proc.env"
    envfile.write_text("".join(f"{k}={v}\n" for k, v in rig.env.items()
                               if k not in ("HOOKSPOOL_STATE_DIR", "HOOKSPOOL_BATCH_ID", "HOOKSPOOL_DELIVERY_ID")))
    return {name: {"argv": [sys.executable, "-m", "hookspool.processor", rig.instance], "cwd": _bobbinrig.ROOT,
                   "env_file": str(envfile), "state_dir": str(rig.state), "timeout_s": 60, "max_inflight": 1}}


def test_end_to_end_through_the_script_destination(rig, make, google, tmp_path):
    pr = make()
    pr.env["HOOKSPOOL_DELTA_ROUTES"] = json.dumps({"scheduling": "hermes:test-route"})
    gseed(pr)
    google.history = [hist("m1", history_id="150")]
    google.messages["m1"] = gmsg("m1")
    r = rig({"destinations": processor_destination(pr, tmp_path)}, destination_factory=build)
    a = r.store.enqueue("gmail", OWNER, "script:mail", gpush(), quiet_s=0, max_s=0)
    r.d.drain_once()
    assert r.batch(a.batch_id)["state"] == "delivered"
    ev = events_for(r, "gmail")
    assert len(ev) == 1 and ev[0]["event_type"] == "scheduling" and ev[0]["destination"] == "hermes:test-route"
    assert ev[0]["key"] == f"{OWNER}:t-m1"
    assert gstate(pr)["historyId"] == "150"


def test_end_to_end_a_failing_pass_retries_and_reports_once(rig, make, google, tmp_path, clock):
    pr = make()
    pr.env["HOOKSPOOL_DELTA_ROUTES"] = json.dumps({"scheduling": "hermes:test-route"})
    gseed(pr)
    google.history = [500, hist("m1", history_id="150")]
    google.messages["m1"] = gmsg("m1")
    r = rig({"destinations": processor_destination(pr, tmp_path)}, destination_factory=build)
    a = r.store.enqueue("gmail", OWNER, "script:mail", gpush(), quiet_s=0, max_s=0)
    r.d.drain_once()
    b = r.batch(a.batch_id)
    assert b["state"] == "retry" and b["last_class"] == "batch" and b["last_error"].startswith("exit 75")
    clock.advance(60)
    r.d.drain_once()
    assert r.batch(a.batch_id)["state"] == "delivered" and len(events_for(r, "gmail")) == 1


def test_end_to_end_a_bad_credential_dead_letters_on_the_first_attempt(rig, make, google, tmp_path):
    pr = make()
    gseed(pr)
    google.history = [403]
    r = rig({"destinations": processor_destination(pr, tmp_path)}, destination_factory=build)
    a = r.store.enqueue("gmail", OWNER, "script:mail", gpush(), quiet_s=0, max_s=0)
    r.d.drain_once()
    b = r.batch(a.batch_id)
    assert b["state"] == "dead" and b["last_class"] == "permanent" and "exit 65" in (b["last_error"] or "")
    r.d.drain_once()
    assert google.count("GET", "/gmail/history") == 1, "dead-lettered at once: no second attempt"


def test_the_processor_runs_under_isolated_mode_the_way_the_dispatcher_starts_it(make, google, tmp_path):
    """The dispatcher spawns ``python -I -m hookspool.processor <instance>``: no working directory, no PYTHON*
    variables. The checkout is not installed in the test environment, so the path is added by a prelude
    that then runs the very module the dispatcher runs."""
    import subprocess
    rig = make()
    gseed(rig)
    google.history = [hist("m1", history_id="150")]
    google.messages["m1"] = gmsg("m1")
    env = {"PATH": os.environ["PATH"], **rig.env, "HOOKSPOOL_BATCH_ID": "5", "HOOKSPOOL_DELIVERY_ID": "hs_gmail_5",
           "HOOKSPOOL_DELTA_ROUTES": json.dumps({"scheduling": "hermes:mail"})}
    prelude = ("import sys, runpy; sys.path.insert(0, %r); sys.argv = ['hookspool.processor', 'gmail']; "
               "runpy.run_module('hookspool.processor', run_name='__main__')" % _bobbinrig.ROOT)
    p = subprocess.run([sys.executable, "-I", "-c", prelude], input=json.dumps(_bobbinrig.batch_doc(gpush(), bid="5", source="gmail")),
                       capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 0, p.stderr
    lines = p.stdout.splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["enqueue"]["event_type"] == "scheduling", "only emit lines reach stdout"
    assert gstate(rig)["historyId"] == "150"
    assert "m1" not in p.stderr and "Availability" not in p.stderr, "logs carry counts, not mail"
