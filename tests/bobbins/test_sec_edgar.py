"""The ``sec.edgar`` poll bobbin (ARCHITECTURE 14.4): the poller through the processor runtime (journal, staged
state, emits) against a scripted EDGAR, with a fake clock and sleep.

* new filings only: the first pass of a feed bootstraps silently, later passes emit what is new, oldest first;
* conditional GET: the ETag and Last-Modified of an answer are saved and sent, and a 304 changes nothing;
* bad XML and refused constructs (a DOCTYPE) skip that feed, and a tick where nothing could be read is retried;
* fair access: no run without a User-Agent that holds an email address, one User-Agent on every request, requests paced;
* failures: 403 is retried later, a 5xx is retried, a bad setting is permanent, a failed pass saves nothing;
* replay of a retried batch, the 14.3.7 rules and the documents of 14.3.8.
"""
from __future__ import annotations

import os

import _bobbinrig
import _bobbinrules as R
import pytest

from hookspool import bobbin_loader as PK
from hookspool import net

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DIR = os.path.join(ROOT, "hookspool", "bobbins", "sec", "edgar")
UA = "Example Research research@example.com"
CIK = "0000000001"
T0 = 1_800_000_000.0


def acc(n: int, year: int = 26) -> str:
    return f"0000000001-{year}-{n:06d}"


def entry(n: int, form: str = "8-K", filed: str = "2026-01-02") -> str:
    a = acc(n)
    return (f'<entry><category label="form type" scheme="https://www.sec.gov/" term="{form}" />'
            f'<content type="text/xml"><accession-number>{a}</accession-number><filing-date>{filed}</filing-date>'
            f'<filing-type>{form}</filing-type></content>'
            f'<id>urn:tag:sec.gov,2008:accession-number={a}</id>'
            f'<link href="https://www.sec.gov/Archives/edgar/data/1/{a}-index.htm" rel="alternate" type="text/html" />'
            f'<title>{form}  - Current report</title><updated>{filed}T10:00:00-05:00</updated></entry>')


def feed(*entries: str, company: str = "Example Corp", cik: str = CIK) -> str:
    return ('<?xml version="1.0" encoding="ISO-8859-1" ?><feed xmlns="http://www.w3.org/2005/Atom">'
            f'<title>{company}  ({cik})</title>' + "".join(entries) + "</feed>")


def ok(text: str, **headers) -> tuple[int, str, dict]:
    return 200, text, {k.replace("_", "-"): v for k, v in headers.items()}


NOT_MODIFIED = (304, "", {})


class Edgar:
    """A scripted EDGAR: ``answers[(cik, form)]`` is a list of answers consumed one per request (the last one repeats);
    an answer is ``(status, text, headers)`` or an exception to raise."""

    def __init__(self, answers: dict | None = None):
        self.answers = answers or {}
        self.calls: list[dict] = []
        self.slept: list[float] = []

    def get_text(self, url, headers=None, *, params=None, bearer=None, timeout_s=None, label=None):
        self.calls.append({"url": url, "headers": dict(headers or {}), "params": dict(params or {})})
        queue = self.answers[(params["CIK"], params["type"])]
        a = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(a, Exception):
            raise a
        return a

    def factory(self, state, resolver):
        return net.make_context_extras(state, resolver, http=self, now=lambda: T0, sleep=self.slept.append)


def make(tmp_path, answers=None, **settings):
    base = {"user_agent": UA, "ciks": [CIK], "forms": ["8-K"], "backfill": 0}
    rig = _bobbinrig.Rig(tmp_path, "sec.edgar", ["filing"], dict(base, **settings), {})
    rig.edgar = Edgar(answers or {})
    return rig


def tick(rig, bid="1", **kw):
    return rig.run({"tick": int(T0), "instance": "edgar"}, bid=bid, clock=lambda: T0,
                   extras_factory=rig.edgar.factory, **kw)


def items(rig, bid="1"):
    rc, emits, err = tick(rig, bid)
    assert rc == 0, err
    return [e["payload"] for e in emits]


# ---- new filings only -------------------------------------------------------------------------------------
def test_the_first_pass_of_a_feed_bootstraps_silently_and_later_passes_emit_what_is_new(tmp_path):
    rig = make(tmp_path, {(CIK, "8-K"): [ok(feed(entry(2), entry(1))), ok(feed(entry(3), entry(2), entry(1)))]})
    assert items(rig, "1") == []
    st = rig.get_state("feeds")[f"{CIK}|8-K"]
    assert st["accession"] == acc(2) and st["seen"] == [acc(2), acc(1)]
    out = items(rig, "2")
    assert [i["accession"] for i in out] == [acc(3)]
    assert out[0] == {"event_type": "filing", "key": CIK, "cik": CIK, "company": "Example Corp", "form": "8-K",
                      "accession": acc(3), "filed": "2026-01-02", "title": "8-K - Current report",
                      "link": f"https://www.sec.gov/Archives/edgar/data/1/{acc(3)}-index.htm"}
    assert items(rig, "3") == []                                  # the same feed again: nothing new


def test_new_filings_come_out_oldest_first(tmp_path):
    rig = make(tmp_path, {(CIK, "8-K"): [ok(feed(entry(1))), ok(feed(entry(4), entry(3), entry(2), entry(1)))]})
    items(rig, "1")
    assert [i["accession"] for i in items(rig, "2")] == [acc(2), acc(3), acc(4)]


def test_backfill_emits_the_newest_few_on_the_first_pass_only(tmp_path):
    rig = make(tmp_path, {(CIK, "8-K"): [ok(feed(entry(3), entry(2), entry(1)))]}, backfill=2)
    assert [i["accession"] for i in items(rig, "1")] == [acc(2), acc(3)]
    assert items(rig, "2") == []


def test_a_company_added_later_bootstraps_alone(tmp_path):
    rig = make(tmp_path, {(CIK, "8-K"): [ok(feed(entry(1)))],
                          ("0000000002", "8-K"): [ok(feed(entry(7), cik="0000000002", company="Other Corp"))]})
    items(rig, "1")
    rig.env["HOOKSPOOL_SETTINGS"] = rig.env["HOOKSPOOL_SETTINGS"].replace('["0000000001"]',
                                                                          '["0000000001", "0000000002"]')
    assert items(rig, "2") == []
    assert set(rig.get_state("feeds")) == {f"{CIK}|8-K", "0000000002|8-K"}


def test_a_filing_two_feeds_match_is_emitted_once(tmp_path):
    both = ok(feed(entry(2, "4/A"), entry(1, "4")))
    after = ok(feed(entry(3, "4/A"), entry(2, "4/A"), entry(1, "4")))
    rig = make(tmp_path, {(CIK, "4"): [both, after], (CIK, "4/A"): [both, after]}, forms=["4", "4/A"])
    items(rig, "1")
    assert [i["accession"] for i in items(rig, "2")] == [acc(3)]


def test_entries_without_an_accession_number_are_dropped(tmp_path):
    odd = '<entry><id>urn:x</id><title>odd</title></entry>'
    rig = make(tmp_path, {(CIK, "8-K"): [ok(feed(entry(1))), ok(feed(odd, entry(2), entry(1)))]})
    items(rig, "1")
    assert [i["accession"] for i in items(rig, "2")] == [acc(2)]


def test_state_of_a_feed_that_is_no_longer_configured_is_dropped(tmp_path):
    rig = make(tmp_path, {(CIK, "8-K"): [ok(feed(entry(1)))], (CIK, "4"): [ok(feed(entry(5, "4")))]},
               forms=["8-K", "4"])
    items(rig, "1")
    assert set(rig.get_state("feeds")) == {f"{CIK}|8-K", f"{CIK}|4"}
    rig.env["HOOKSPOOL_SETTINGS"] = rig.env["HOOKSPOOL_SETTINGS"].replace('["8-K", "4"]', '["8-K"]')
    items(rig, "2")
    assert set(rig.get_state("feeds")) == {f"{CIK}|8-K"}


# ---- conditional GET --------------------------------------------------------------------------------------
def test_the_etag_and_last_modified_are_saved_and_sent_and_a_304_changes_nothing(tmp_path):
    rig = make(tmp_path, {(CIK, "8-K"): [ok(feed(entry(1)), etag='"v1"', last_modified="Mon, 05 Jan 2026 10:00:00 GMT"),
                                         NOT_MODIFIED]})
    items(rig, "1")
    assert "If-None-Match" not in rig.edgar.calls[0]["headers"]
    saved = rig.get_state("feeds")
    assert saved[f"{CIK}|8-K"]["etag"] == '"v1"'
    assert saved[f"{CIK}|8-K"]["modified"] == "Mon, 05 Jan 2026 10:00:00 GMT"
    assert items(rig, "2") == []
    h = rig.edgar.calls[1]["headers"]
    assert h["If-None-Match"] == '"v1"' and h["If-Modified-Since"] == "Mon, 05 Jan 2026 10:00:00 GMT"
    assert rig.get_state("feeds") == saved                         # a 304 keeps the feed's state exactly


def test_a_feed_without_validators_is_read_every_time(tmp_path):
    rig = make(tmp_path, {(CIK, "8-K"): [ok(feed(entry(1)))]})
    items(rig, "1")
    items(rig, "2")
    assert all("If-None-Match" not in c["headers"] and "If-Modified-Since" not in c["headers"] for c in rig.edgar.calls)


def test_a_304_for_a_feed_with_no_state_is_not_an_error(tmp_path):
    rig = make(tmp_path, {(CIK, "8-K"): [NOT_MODIFIED]})
    assert items(rig) == []


# ---- bad documents ----------------------------------------------------------------------------------------
BOMB = ('<?xml version="1.0"?><!DOCTYPE feed [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>&b;</title></feed>')
XXE = ('<?xml version="1.0"?><!DOCTYPE feed [<!ENTITY x SYSTEM "file:///etc/hostname">]>'
       '<feed xmlns="http://www.w3.org/2005/Atom"><title>&x;</title></feed>')


@pytest.mark.parametrize("bad", ["<html><body>Maintenance</body></html>", "<feed", "", BOMB, XXE],
                         ids=["html", "truncated", "empty", "entity-bomb", "external-entity"])
def test_a_bad_feed_is_skipped_and_the_others_still_run(tmp_path, bad):
    rig = make(tmp_path, {(CIK, "8-K"): [ok(feed(entry(1))), ok(feed(entry(2), entry(1)))],
                          ("0000000002", "8-K"): [ok(bad)]}, ciks=[CIK, "0000000002"])
    assert items(rig, "1") == []
    assert [i["accession"] for i in items(rig, "2")] == [acc(2)]
    assert set(rig.get_state("feeds")) == {f"{CIK}|8-K"}


def test_a_bad_feed_keeps_the_state_it_had(tmp_path):
    rig = make(tmp_path, {(CIK, "8-K"): [ok(feed(entry(1)), etag='"v1"'), ok("<html/>")],
                          ("0000000002", "8-K"): [ok(feed(entry(7), cik="0000000002"))]}, ciks=[CIK, "0000000002"])
    items(rig, "1")
    before = rig.get_state("feeds")[f"{CIK}|8-K"]
    items(rig, "2")
    assert rig.get_state("feeds")[f"{CIK}|8-K"] == before


def test_when_no_feed_can_be_read_the_tick_is_retried_and_saves_nothing(tmp_path):
    rig = make(tmp_path, {(CIK, "8-K"): [ok("<html/>")]})
    rc, _emits, err = tick(rig)
    assert rc == 75 and "failed (Retryable)" in err
    assert not rig.has_state("feeds")


def test_a_feed_that_is_not_found_is_skipped(tmp_path):
    gone = net.Permanent("GET www.sec.gov feed: HTTP 404", status=404)
    rig = make(tmp_path, {(CIK, "8-K"): [ok(feed(entry(1)))], ("0000000002", "8-K"): [gone]}, ciks=[CIK, "0000000002"])
    assert items(rig) == []
    assert set(rig.get_state("feeds")) == {f"{CIK}|8-K"}


# ---- fair access ------------------------------------------------------------------------------------------
@pytest.mark.parametrize("ua", ["", "   ", "Example Research", "research@", "no-email-here.example.com", "a@b.co\nX: y"])
def test_it_refuses_to_run_without_a_user_agent_that_has_an_email_address(tmp_path, ua):
    rig = make(tmp_path, {(CIK, "8-K"): [ok(feed(entry(1)))]}, user_agent=ua)
    rc, _emits, err = tick(rig)
    assert rc == 65 and "user_agent" in err
    assert rig.edgar.calls == []


def test_a_missing_user_agent_setting_never_reaches_edgar(tmp_path):
    rig = make(tmp_path, {(CIK, "8-K"): [ok(feed(entry(1)))]})
    rig.env["HOOKSPOOL_SETTINGS"] = '{"ciks": ["0000000001"], "forms": ["8-K"], "backfill": 0}'
    rc, _emits, _err = tick(rig)
    assert rc != 0 and rig.edgar.calls == []                      # (hookspool check refuses the config before this)


def test_every_request_carries_the_user_agent_and_the_documented_query(tmp_path):
    rig = make(tmp_path, {(CIK, "8-K"): [ok(feed(entry(1)))], ("0000000002", "8-K"): [ok(feed(entry(2), cik="0000000002"))]},
               ciks=[1, "2"])
    items(rig)
    assert [c["headers"]["User-Agent"] for c in rig.edgar.calls] == [UA, UA]
    assert rig.edgar.calls[0]["url"] == "https://www.sec.gov/cgi-bin/browse-edgar"
    assert rig.edgar.calls[0]["params"] == {"action": "getcompany", "CIK": CIK, "type": "8-K", "dateb": "",
                                            "owner": "include", "count": 40, "output": "atom"}
    assert rig.edgar.calls[1]["params"]["CIK"] == "0000000002"


def test_requests_are_paced_by_core_and_the_bobbin_never_sleeps(tmp_path):
    forms = ["8-K", "4", "10-K", "10-Q"]
    rig = make(tmp_path, {(CIK, f): [ok(feed(entry(1)))] for f in forms}, forms=forms)
    items(rig)
    assert len(rig.edgar.calls) == 4
    # the fake clock stands still, so each request waits one more interval: 5 a second, far under EDGAR's 10
    assert rig.edgar.slept == pytest.approx([0.2, 0.4, 0.6], abs=0.01)
    assert "sleep" not in open(os.path.join(DIR, "poller.py")).read().replace("never sleep", "")


@pytest.mark.parametrize("settings", [
    {"ciks": []}, {"ciks": ["abc"]}, {"ciks": ["12345678901"]}, {"ciks": [True]}, {"forms": []}, {"forms": ["8 K;rm"]},
    {"forms": [5]}, {"backfill": -1}, {"backfill": 41}, {"ciks": [str(i) for i in range(1, 102)],
                                                                             "forms": ["8-K", "4"]}])
def test_bad_settings_are_permanent_and_make_no_request(tmp_path, settings):
    rig = make(tmp_path, {(CIK, "8-K"): [ok(feed(entry(1)))]}, **settings)
    rc, _emits, _err = tick(rig)
    assert rc == 65 and rig.edgar.calls == []


def test_ciks_may_be_numbers_and_are_zero_padded_and_deduplicated(tmp_path):
    rig = make(tmp_path, {(CIK, "8-K"): [ok(feed(entry(1)))]}, ciks=[1, "1", "0000000001"], forms=["8-K", " 8-K "])
    items(rig)
    assert len(rig.edgar.calls) == 1 and rig.edgar.calls[0]["params"]["CIK"] == CIK


def test_a_403_is_retried_later_with_a_delay(tmp_path):
    blocked = net.Permanent("GET www.sec.gov feed: HTTP 403", status=403)
    rig = make(tmp_path, {(CIK, "8-K"): [blocked]})
    rc, _emits, err = tick(rig)
    assert rc == 75 and "failed (Retryable)" in err
    assert not rig.has_state("feeds")


def test_a_server_error_is_retried_and_a_failed_pass_changes_no_state(tmp_path):
    down = net.Retryable("GET www.sec.gov feed: HTTP 503", status=503)
    rig = make(tmp_path, {(CIK, "8-K"): [ok(feed(entry(1))), down], ("0000000002", "8-K"): [down]}, ciks=[CIK, "0000000002"])
    rc, _e, _err = tick(rig, "1")
    assert rc == 75 and not rig.has_state("feeds")


def test_another_permanent_status_is_permanent(tmp_path):
    rig = make(tmp_path, {(CIK, "8-K"): [net.Permanent("GET www.sec.gov feed: HTTP 400", status=400)]})
    rc, _e, _err = tick(rig)
    assert rc == 65


# ---- replay -----------------------------------------------------------------------------------------------
def test_a_retried_batch_replays_the_journal_without_asking_edgar_again(tmp_path):
    rig = make(tmp_path, {(CIK, "8-K"): [ok(feed(entry(1))), ok(feed(entry(2), entry(1)))]})
    items(rig, "1")
    first = tick(rig, "2")
    n = len(rig.edgar.calls)
    second = tick(rig, "2")
    assert first[0] == second[0] == 0 and first[1] == second[1] and len(first[1]) == 1
    assert len(rig.edgar.calls) == n and "replaying the journal" in second[2]


# ---- the rules and the documents ---------------------------------------------------------------------------
def test_the_poller_calls_core_only():
    assert R.violations(DIR) == []


def test_the_manifest_is_a_poll_bobbin_with_no_secret_and_the_documented_settings():
    pkgs, errs = PK.discover({})
    assert errs == [], errs
    pkg = pkgs["sec.edgar"]
    assert pkg.errors == [] and pkg.builtin and pkg.poll["every_s"] == 300 and pkg.processor is None
    assert pkg.secrets == {} and set(pkg.settings) == {"user_agent", "ciks", "forms", "backfill"}
    assert pkg.settings["user_agent"].get("required") is True and "default" not in pkg.settings["user_agent"]
    assert pkg.settings["forms"]["default"] == ["8-K", "4"] and set(pkg.outputs) == {"filing"}
    prov = pkg.provider
    assert prov["no_push_note"] == "EDGAR publishes RSS and Atom feeds only; there is no push."
    assert prov["no_push_docs"] == prov["api_docs"] and "webhook_docs" not in prov
