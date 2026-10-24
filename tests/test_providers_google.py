"""``hookspool.providers.google`` (ARCHITECTURE 14.3.7): the code the Gmail, Calendar and Drive bobbins share.

* the push-channel rules: the token header, the ``sync`` handshake, the header-built payload, the resource id
  inside the resource URI (a calendar id for Calendar, a file id for Drive), the dedup id;
* the API calls, through a stub client: history paging and ``HistoryGone``, ``SyncGone``, a deleted message,
  free/busy, and message parsing.
"""
from __future__ import annotations

import base64

import pytest

from hookspool import net
from hookspool.plugin import Request
from hookspool.providers import google as G

Permanent, Retryable = net.Permanent, net.Retryable


def req(headers: dict | None = None, query: str = "") -> Request:
    return Request("POST", "/hooks/x" + query, list((headers or {}).items()), b"", now=0)


# ---- push channels ---------------------------------------------------------------------------------------------
def test_the_channel_token_is_compared_exactly():
    r = req({"X-Goog-Channel-Token": "tok-1"})
    assert G.verify_channel(r, "tok-1") is True
    assert G.verify_channel(r, "tok-2") is False and G.verify_channel(r, "tok-1 ") is False
    assert G.verify_channel(req(), "tok-1") is False
    assert G.verify_channel(req({"X-Goog-Channel-Token": ""}), "") is False, "an empty token authenticates nobody"
    assert G.CHANNEL_AUTH_HEADERS == ("X-Goog-Channel-Token",)


def test_the_sync_ping_is_the_only_handshake():
    assert G.ignore_handshake(req({"X-Goog-Resource-State": "sync"})) == "sync"
    assert G.ignore_handshake(req({"X-Goog-Resource-State": "exists"})) is None
    assert G.ignore_handshake(req()) is None


def test_the_payload_is_built_from_the_headers_for_each_source():
    r = req({"X-Goog-Channel-ID": "c1", "X-Goog-Resource-ID": "r1", "X-Goog-Resource-State": "update",
             "X-Goog-Resource-URI": "https://www.googleapis.com/drive/v3/files/F1?alt=json",
             "X-Goog-Message-Number": "7"})
    p = G.channel_payload(r, "google-drive")
    assert p == {"event_type": "update", "channel_id": "c1", "resource_id": "r1", "resource_state": "update",
                 "resource_uri": "https://www.googleapis.com/drive/v3/files/F1?alt=json", "message_number": "7",
                 "source": "google-drive"}
    assert G.channel_payload(req(), "google-calendar")["resource_state"] == "unknown"


def test_the_resource_id_comes_from_the_uri_for_calendars_and_drive_files():
    cal = "https://www.googleapis.com/calendar/v3/calendars/team%40example.test/events?alt=json"
    assert G.resource_segment(cal, "calendars") == "team@example.test"
    assert G.resource_segment("https://www.googleapis.com/drive/v3/files/F1?alt=json", "files") == "F1"
    assert G.resource_segment("https://www.googleapis.com/drive/v3/changes?alt=json", "files") is None
    assert G.resource_segment(None, "calendars") is None and G.resource_segment("", "files") is None
    assert G.resource_segment(cal, "files") is None


def test_the_dedup_id_is_the_channel_and_message_number():
    assert G.channel_dedup_id({"channel_id": "c1", "message_number": "7"}) == "c1:7"
    assert G.channel_dedup_id({"channel_id": "c1", "message_number": ""}) is None
    assert G.channel_dedup_id({}) is None


# ---- API calls through a stub client ----------------------------------------------------------------------------------
class Stub:
    """Answers ``get``/``post``/``request`` from a script of responses; an int is an HTTP error status."""

    def __init__(self, *responses):
        self.script = list(responses)
        self.calls = []

    def _next(self, method, url, kw):
        self.calls.append((method, url, kw))
        r = self.script.pop(0)
        if isinstance(r, int):
            raise (Retryable if r >= 500 else Permanent)(f"{method} host: HTTP {r}", status=r)
        return r

    def get(self, url, **kw):
        return self._next("GET", url, kw)

    def post(self, url, **kw):
        return self._next("POST", url, kw)

    def request(self, method, url, **kw):
        return self._next(method, url, kw)


def test_history_is_paged_deduplicated_and_ordered():
    c = Stub({"history": [{"messagesAdded": [{"message": {"id": "a"}}, {"message": {"id": "b"}}]}],
              "nextPageToken": "p2", "historyId": "11"},
             {"history": [{"messagesAdded": [{"message": {"id": "b"}}, {"message": {"id": "c"}}]}, {"labelsAdded": []}],
              "historyId": "12"})
    ids, newest = G.gmail_history(c, "10", base="https://gmail.example.test/v1/users/me")
    assert ids == ["a", "b", "c"] and newest == "12"
    (_, url1, kw1), (_, url2, kw2) = c.calls
    assert url1 == "https://gmail.example.test/v1/users/me/history" and kw1["params"]["startHistoryId"] == "10"
    assert kw1["params"]["labelId"] == "INBOX" and "pageToken" not in kw1["params"]
    assert kw2["params"]["pageToken"] == "p2"


def test_a_stale_history_id_is_history_gone_and_other_errors_pass_through():
    with pytest.raises(G.HistoryGone):
        G.gmail_history(Stub(404), "10")
    with pytest.raises(Permanent) as e:
        G.gmail_history(Stub(403), "10")
    assert e.value.status == 403 and e.value.__cause__ is None
    with pytest.raises(Retryable):
        G.gmail_history(Stub(503), "10")


def test_history_pages_are_bounded():
    c = Stub(*[{"history": [], "nextPageToken": "more", "historyId": str(i)} for i in range(25)])
    stats: dict = {}
    ids, newest = G.gmail_history(c, "1", max_pages=3, stats=stats)
    assert ids == [] and len(c.calls) == 3 and newest == "2"
    assert stats == {"pages": 3, "records": 0, "truncated": True}
    stats = {}
    G.gmail_history(Stub({"history": [{}, {}], "historyId": "9"}), "1", stats=stats)
    assert stats == {"pages": 1, "records": 2, "truncated": False}


def test_a_message_that_is_gone_is_none_and_the_id_is_quoted():
    assert G.gmail_message(Stub(404), "x") is None
    c = Stub({"id": "a/b"})
    assert G.gmail_message(c, "a/b")["id"] == "a/b"
    assert c.calls[0][1].endswith("/messages/a%2Fb") and c.calls[0][2]["params"] == {"format": "full"}
    with pytest.raises(Permanent):
        G.gmail_message(Stub(403), "x")


def test_events_are_paged_and_a_gone_sync_token_is_sync_gone():
    c = Stub({"items": [{"id": "a"}], "nextPageToken": "p2"}, {"items": [{"id": "b"}], "nextSyncToken": "s1"})
    items, sync = G.calendar_events(c, {"syncToken": "s0"}, url="https://cal.example.test/events")
    assert [i["id"] for i in items] == ["a", "b"] and sync == "s1"
    assert c.calls[1][2]["params"] == {"syncToken": "s0", "pageToken": "p2"}
    with pytest.raises(G.SyncGone):
        G.calendar_events(Stub(410), {"syncToken": "old"})
    with pytest.raises(Permanent):
        G.calendar_events(Stub(403), {})
    assert G.calendar_events(Stub({"items": [{"id": "z"}]}), {})[1] == "", "no sync token on the last page: empty"


def test_events_are_inserted_and_deleted_with_the_event_id_quoted():
    c = Stub({"id": "new"}, {})
    assert G.calendar_insert(c, {"summary": "s"}, url="https://cal.example.test/events")["id"] == "new"
    G.calendar_delete(c, "ev/1", url="https://cal.example.test/events")
    assert c.calls[0][:2] == ("POST", "https://cal.example.test/events") and c.calls[0][2]["json"] == {"summary": "s"}
    assert c.calls[1][:2] == ("DELETE", "https://cal.example.test/events/ev%2F1")


def test_freebusy_returns_sorted_aware_intervals():
    from datetime import datetime, timezone
    c = Stub({"calendars": {"primary": {"busy": [{"start": "2026-10-30T13:00:00Z", "end": "2026-10-30T14:00:00Z"},
                                                 {"start": "2026-10-30T10:00:00Z", "end": "2026-10-30T11:00:00Z"}]},
                            "other": {"busy": []}}})
    t0 = datetime(2026, 10, 30, tzinfo=timezone.utc)
    busy = G.freebusy(c, t0, t0.replace(day=31), "UTC", url="https://fb.example.test/x", calendar_ids=("primary", "other"))
    assert [b[0].hour for b in busy] == [10, 13] and all(b[0].tzinfo is not None for b in busy)
    body = c.calls[0][2]["json"]
    assert body["items"] == [{"id": "primary"}, {"id": "other"}] and body["timeZone"] == "UTC"
    assert body["timeMin"] == t0.isoformat()


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


RAW = {"id": "m1", "threadId": "t1", "labelIds": ["INBOX", "UNREAD"], "internalDate": "1700000000000", "snippet": "snip",
       "payload": {"mimeType": "multipart/mixed", "headers": [
           {"name": "From", "value": "Pat <PAT@Sender.example.test>"}, {"name": "Subject", "value": "Hello"},
           {"name": "subject", "value": "second subject header"}, {"name": "Message-ID", "value": "<m1@x>"}],
           "parts": [{"mimeType": "multipart/alternative", "parts": [
               {"mimeType": "text/html", "body": {"data": b64url("<p>html</p>")}},
               {"mimeType": "text/plain", "body": {"data": b64url("line one\r\nline two é")}}]}]}}


def test_a_message_is_parsed_into_the_shape_hooks_get():
    m = G.parse_message(RAW)
    assert m == {"id": "m1", "thread_id": "t1", "labels": ["INBOX", "UNREAD"], "snippet": "snip",
                 "internal_date": 1700000000000, "body": "line one\nline two é",
                 "headers": {"from": "Pat <PAT@Sender.example.test>", "subject": "Hello", "message-id": "<m1@x>"}}
    assert G.address("Pat <PAT@Sender.example.test>") == "pat@sender.example.test"
    assert G.address(" Bare@Example.test ") == "bare@example.test" and G.address("") == ""
    assert G.header_of(RAW, "SUBJECT") == "Hello" and G.header_of(RAW, "x-none") == ""


def test_the_body_falls_back_to_the_snippet_and_a_bad_message_does_not_raise():
    assert G.body_text({"snippet": "only a snippet", "payload": {"mimeType": "text/html"}}) == "only a snippet"
    m = G.parse_message({"id": "x", "internalDate": "not a number"})
    assert m["internal_date"] == 0 and m["labels"] == [] and m["headers"] == {} and m["body"] == ""
