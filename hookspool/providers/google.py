"""Google building blocks shared by the ``google.gmail``, ``google.calendar``, ``google.drive``, ``google.meet`` and
``google.tasks`` bobbins (ARCHITECTURE 14.3.7).

Two halves, so that each bobbin stays a thin description of its own service.

**Pub/Sub push (source side).** Gmail and Meet both deliver through a Cloud Pub/Sub push subscription. Pub/Sub cannot
sign a push, so the subscription's endpoint URL carries a long random ``?token=...`` (an ``X-Push-Token`` header is
accepted too, for a proxy that moves it), compared in constant time by ``verify_push``. ``push_message`` unwraps the
envelope (``{"message": {"data": <base64 JSON>, "attributes": {...}, "messageId", "publishTime"}, "subscription"}``)
or raises ``BadPayload``, so a push that does not decode is quarantined, never dropped.

**Push channels (source side).** Calendar and Drive both use Google's watch channel: an empty POST with
``X-Goog-*`` headers, authenticated only by the ``token`` the channel was created with, which Google echoes
in ``X-Goog-Channel-Token``. ``verify_channel``, ``ignore_handshake`` (the ``sync`` ping Google sends when a
channel is created), ``channel_payload`` (the payload built from the headers), ``resource_segment`` (the
calendar or file id inside ``X-Goog-Resource-URI``) and ``channel_dedup_id`` are that contract, once.

**API calls (processor side).** Functions that take an ``OAuthClient`` (``ctx.oauth(secret)``): token
refresh, bearer header, timeouts, retries and the ``Retryable`` / ``Permanent`` mapping are the client's
(``hookspool/net.py``). What is added here is only what Google's APIs mean: Gmail history paging and message
parsing, Calendar sync-token listing, event insert and delete, free/busy, the Drive change feed (a start
page token, then changes from a stored page token), the Tasks API (a user's task lists, and one list's tasks modified
since a time, completed, hidden and deleted included), and a Meet conference record with its participants and their
sessions (Meet REST v2). Four answers are not failures and become exceptions the
caller handles: ``HistoryGone`` (Gmail answered 404 to a stale history id), ``SyncGone`` (Calendar answered 410
to an expired sync token), ``PageTokenGone`` (Drive answered 404 or 410 to a stored page token) and ``TasklistGone``
(Tasks answered 404 for a list that was just deleted); the first three mean "start again from a fresh bootstrap",
the last "leave that list out". Everything else that is not 2xx is raised by the client as ``Retryable`` or ``Permanent``.

Base URLs are parameters, so a deployment (or a test) can point a processor elsewhere.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import quote, unquote

from ..net import Permanent
from ..plugin import BadPayload, Request, verify as v

__all__ = ["PUSH_TOKEN_PARAM", "PUSH_TOKEN_HEADER", "PUSH_AUTH_HEADERS", "verify_push", "push_message",
           "CHANNEL_TOKEN_HEADER", "CHANNEL_AUTH_HEADERS", "verify_channel", "resource_state", "ignore_handshake",
           "channel_payload", "resource_segment", "channel_dedup_id", "GMAIL_API", "CALENDAR_EVENTS_API",
           "FREEBUSY_API", "DRIVE_API", "HistoryGone", "SyncGone", "PageTokenGone", "gmail_profile", "gmail_history", "gmail_message",
           "parse_message", "body_text", "address", "calendar_events", "calendar_insert", "calendar_delete",
           "freebusy", "drive_start_page_token", "drive_changes", "CHANGE_FIELDS",
           "TASKS_API", "TasklistGone", "tasks_lists", "tasks_changes",
           "MEET_API", "MEET_RECORD_RE", "meet_conference", "meet_participants", "meet_sessions", "meet_participant",
           "meet_attendance"]

# ---- Pub/Sub push (source side) ----------------------------------------------------------------------
PUSH_TOKEN_PARAM = "token"
PUSH_TOKEN_HEADER = "X-Push-Token"
PUSH_AUTH_HEADERS = (PUSH_TOKEN_HEADER,)               # never stored; the query string is never stored either


def verify_push(req: Request, token: str) -> bool:
    """The push token of the subscription's endpoint URL (``?token=``, else the ``X-Push-Token`` header), compared
    in constant time. The query value wins when both are present."""
    return v.static_token(req.query.get(PUSH_TOKEN_PARAM) or req.header(PUSH_TOKEN_HEADER), token)


def _push_data(data: object) -> dict:
    if not isinstance(data, str) or not data:
        raise BadPayload("pub/sub message has no data")
    try:
        raw = base64.b64decode(data + "=" * (-len(data) % 4), validate=False)
        inner = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise BadPayload("pub/sub data is not base64 JSON") from None
    if not isinstance(inner, dict):
        raise BadPayload("pub/sub data is not an object")
    return inner


def push_message(req: Request) -> dict:
    """The Pub/Sub push envelope of ``req``, unwrapped::

        {"data": <the decoded JSON object>, "attributes": {name: value} (strings only),
         "messageId", "publishTime", "subscription"}    # the last three only when Pub/Sub sent them

    Raises ``BadPayload`` when the body is not an envelope or its data is not base64 JSON of an object."""
    env = req.json()
    msg = env.get("message") if isinstance(env, dict) else None
    if not isinstance(msg, dict):
        raise BadPayload("not a pub/sub push envelope")
    out: dict[str, Any] = {"data": _push_data(msg.get("data"))}
    attrs = msg.get("attributes")
    out["attributes"] = ({str(k): val for k, val in attrs.items() if isinstance(val, str)}
                         if isinstance(attrs, dict) else {})
    for f in ("messageId", "publishTime"):
        if isinstance(msg.get(f), str) and msg[f]:
            out[f] = msg[f]
    if isinstance(env.get("subscription"), str) and env["subscription"]:
        out["subscription"] = env["subscription"]
    return out


# ---- push channels (source side) ----------------------------------------------------------------------
CHANNEL_TOKEN_HEADER = "X-Goog-Channel-Token"
CHANNEL_AUTH_HEADERS = (CHANNEL_TOKEN_HEADER,)          # never stored, whatever the source keeps
SYNC_STATE = "sync"


def verify_channel(req: Request, token: str) -> bool:
    """The channel token Google echoes, compared in constant time."""
    return v.static_token(req.header(CHANNEL_TOKEN_HEADER), token)


def resource_state(req: Request) -> str:
    return req.header("X-Goog-Resource-State") or "unknown"


def ignore_handshake(req: Request) -> str | None:
    """``"sync"`` for the ping Google sends when a channel is created (verified, acknowledged, not
    enqueued), else None."""
    return SYNC_STATE if req.header("X-Goog-Resource-State") == SYNC_STATE else None


def channel_payload(req: Request, source: str) -> dict:
    """The payload of a push, built from its headers (there is no body)."""
    state = resource_state(req)
    return {
        "event_type": state,
        "channel_id": req.header("X-Goog-Channel-ID"),
        "resource_id": req.header("X-Goog-Resource-ID"),
        "resource_state": state,
        "resource_uri": req.header("X-Goog-Resource-URI"),
        "message_number": req.header("X-Goog-Message-Number"),
        "source": source,
    }


def resource_segment(uri: str | None, segment: str) -> str | None:
    """The id that follows ``/<segment>/`` in a resource URI (``calendars`` -> the calendar id,
    ``files`` -> a Drive file id), percent-decoded, or None."""
    m = re.search(rf"/{re.escape(segment)}/([^/?]+)", uri or "")
    return unquote(m.group(1)) if m else None


def channel_dedup_id(payload: dict) -> str | None:
    """``<channel>:<message number>``: the number counts up per channel and Google resends the same
    number when it retries a push."""
    ch, n = payload.get("channel_id"), payload.get("message_number")
    return f"{ch}:{n}" if ch and n else None


# ---- API calls (processor side) -----------------------------------------------------------------------
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me/"
CALENDAR_EVENTS_API = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
FREEBUSY_API = "https://www.googleapis.com/calendar/v3/freeBusy"
DRIVE_API = "https://www.googleapis.com/drive/v3/"
HISTORY_PAGES = 20
EVENT_PAGES = 40
CHANGE_PAGES = 50
CHANGE_PAGE_SIZE = 1000
# What a change entry carries, and no more: the file's identity and who touched it, never its contents.
CHANGE_FIELDS = ("nextPageToken,newStartPageToken,changes(fileId,removed,time,"
                 "file(id,name,mimeType,createdTime,modifiedTime,sharedWithMeTime,trashed,webViewLink,"
                 "lastModifyingUser(emailAddress,displayName),sharingUser(emailAddress,displayName)))")


class HistoryGone(Exception):
    """Gmail no longer has the stored history id (HTTP 404): bootstrap again."""


class SyncGone(Exception):
    """Calendar no longer accepts the stored sync token (HTTP 410): bootstrap again."""


class PageTokenGone(Exception):
    """Drive no longer accepts the stored page token (HTTP 404 or 410): bootstrap again."""


def _status_of(call) -> tuple[Any, int | None]:
    """(result, None), or (None, the HTTP status of a ``Permanent`` the call raised). The error itself is
    dropped here and re-raised by the caller outside this block, so nothing is chained to it."""
    try:
        return call(), None
    except Permanent as e:
        if e.status is None:
            raise
        return e, e.status


def _gmail_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path


def gmail_profile(client, base: str = GMAIL_API) -> dict:
    return client.get(_gmail_url(base, "profile"), label="gmail.profile")


def gmail_history(client, start_history_id: str, *, base: str = GMAIL_API, label_id: str = "INBOX",
                  max_pages: int = HISTORY_PAGES, stats: dict | None = None) -> tuple[list[str], str]:
    """(ids of messages added to ``label_id`` since ``start_history_id`` in order, the newest history id).
    Raises ``HistoryGone`` when Gmail no longer has the start id.

    At most ``max_pages`` pages are read. When Gmail still had more, the newest id returned skips the rest of the
    gap; ``stats``, when given, receives ``pages`` (read), ``records`` (history records read) and ``truncated``
    (True in that case) so the caller can log it."""
    ids: list[str] = []
    page, newest = None, start_history_id
    pages = records = 0
    for _ in range(max_pages):
        params = {"startHistoryId": start_history_id, "historyTypes": "messageAdded", "labelId": label_id,
                  "maxResults": 500}
        if page:
            params["pageToken"] = page
        h, status = _status_of(lambda: client.get(_gmail_url(base, "history"), params=params, label="gmail.history"))
        if status == 404:
            raise HistoryGone()
        if status is not None:
            raise h
        newest = h.get("historyId", newest)
        pages += 1
        for rec in h.get("history") or []:
            records += 1
            for a in rec.get("messagesAdded") or []:
                mid = (a.get("message") or {}).get("id")
                if mid and mid not in ids:
                    ids.append(mid)
        page = h.get("nextPageToken")
        if not page:
            break
    if stats is not None:
        stats.update(pages=pages, records=records, truncated=bool(page))
    return ids, newest


def gmail_message(client, message_id: str, base: str = GMAIL_API) -> dict | None:
    """The full message, or None when it is gone (404: deleted after the history entry was written)."""
    url = _gmail_url(base, "messages/" + quote(message_id, safe=""))
    m, status = _status_of(lambda: client.get(url, params={"format": "full"}, label="gmail.message"))
    if status == 404:
        return None
    if status is not None:
        raise m
    return m


def header_of(raw: dict, name: str) -> str:
    """The first header called ``name`` (case-insensitive) of a raw Gmail message, or ""."""
    for h in (raw.get("payload") or {}).get("headers") or []:
        if str(h.get("name", "")).lower() == name.lower():
            return h.get("value", "")
    return ""


def body_text(raw: dict) -> str:
    """The first ``text/plain`` part of a raw Gmail message, decoded; the snippet when there is none."""
    def walk(p):
        if not p:
            return ""
        if p.get("mimeType") == "text/plain" and (p.get("body") or {}).get("data"):
            return base64.urlsafe_b64decode(p["body"]["data"] + "==").decode("utf-8", "replace")
        for c in p.get("parts") or []:
            t = walk(c)
            if t:
                return t
        return ""
    t = walk(raw.get("payload"))
    return re.sub(r"\r", "", t) if t else (raw.get("snippet") or "")


def address(value: str) -> str:
    """The lower-cased address of a ``Name <a@b>`` header value."""
    m = re.search(r"<([^>]+)>", value or "")
    return (m.group(1) if m else (value or "")).strip().lower()


def parse_message(raw: dict) -> dict:
    """A raw Gmail message as the plain dict hooks and processors work with::

        {"id", "thread_id", "labels": [...], "headers": {lower-cased name: first value},
         "body": text, "snippet", "internal_date": milliseconds since the epoch (int)}
    """
    headers: dict[str, str] = {}
    for h in (raw.get("payload") or {}).get("headers") or []:
        name = str(h.get("name", "")).lower()
        if name and name not in headers:
            headers[name] = h.get("value", "")
    try:
        internal = int(raw.get("internalDate") or 0)
    except (TypeError, ValueError):
        internal = 0
    return {"id": raw.get("id"), "thread_id": raw.get("threadId"), "labels": list(raw.get("labelIds") or []),
            "headers": headers, "body": body_text(raw), "snippet": raw.get("snippet") or "",
            "internal_date": internal}


def calendar_events(client, params: dict, *, url: str = CALENDAR_EVENTS_API,
                    max_pages: int = EVENT_PAGES) -> tuple[list[dict], str]:
    """(every item of the listing, the ``nextSyncToken`` of its last page or ""). Raises ``SyncGone`` when
    Calendar rejects the sync token in ``params`` (HTTP 410)."""
    items: list[dict] = []
    page, sync = None, ""
    for _ in range(max_pages):
        p = dict(params)
        if page:
            p["pageToken"] = page
        d, status = _status_of(lambda: client.get(url, params=p, label="calendar.events"))
        if status == 410:
            raise SyncGone()
        if status is not None:
            raise d
        items.extend(d.get("items") or [])
        page = d.get("nextPageToken")
        if not page:
            sync = d.get("nextSyncToken", "")
            break
    return items, sync


def calendar_insert(client, body: dict, *, url: str = CALENDAR_EVENTS_API) -> dict:
    return client.post(url, json=body, label="calendar.insert")


def calendar_delete(client, event_id: str, *, url: str = CALENDAR_EVENTS_API) -> None:
    client.request("DELETE", url + "/" + quote(event_id, safe=""), label="calendar.delete")


def freebusy(client, time_min: datetime, time_max: datetime, tz_name: str, *, url: str = FREEBUSY_API,
             calendar_ids: tuple[str, ...] = ("primary",)) -> list[tuple[datetime, datetime]]:
    """Busy intervals (timezone-aware, oldest first) of ``calendar_ids`` between two instants."""
    body = {"timeMin": time_min.isoformat(), "timeMax": time_max.isoformat(), "timeZone": tz_name,
            "items": [{"id": c} for c in calendar_ids]}
    fb = client.post(url, json=body, label="calendar.freebusy")
    busy = []
    for cal in (fb.get("calendars") or {}).values():
        for b in cal.get("busy") or []:
            busy.append((datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])))
    busy.sort()
    return busy


def _drive_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path


def _drive_scope(drive_id: str) -> dict:
    """The query parameters that make a change feed cover one shared drive instead of the user's own."""
    if not drive_id:
        return {}
    return {"driveId": drive_id, "includeItemsFromAllDrives": "true", "supportsAllDrives": "true"}


def drive_start_page_token(client, *, base: str = DRIVE_API, drive_id: str = "") -> str:
    """The page token that stands for "now" in the change feed (``changes.getStartPageToken``)."""
    params = _drive_scope(drive_id)
    d = client.get(_drive_url(base, "changes/startPageToken"), params=params, label="drive.start_token")
    token = d.get("startPageToken") if isinstance(d, dict) else None
    return str(token) if token else ""


def drive_changes(client, page_token: str, *, base: str = DRIVE_API, drive_id: str = "",
                  max_pages: int = CHANGE_PAGES) -> tuple[list[dict], str, bool]:
    """(the changes since ``page_token`` in order, the token to store next, whether the feed was read to its end).

    The token to store is the feed's ``newStartPageToken`` when the last page was read; when ``max_pages``
    ran out first it is the ``nextPageToken`` the read stopped at, so the next pass carries on from there
    (the third value is then False). Raises ``PageTokenGone`` when Drive rejects the stored token."""
    changes: list[dict] = []
    token = page_token
    for _ in range(max_pages):
        params = {"pageToken": token, "pageSize": CHANGE_PAGE_SIZE, "includeRemoved": "true",
                  "fields": CHANGE_FIELDS, **_drive_scope(drive_id)}
        d, status = _status_of(lambda: client.get(_drive_url(base, "changes"), params=params, label="drive.changes"))
        if status in (404, 410):
            raise PageTokenGone()
        if status is not None:
            raise d
        changes.extend(d.get("changes") or [])
        nxt = d.get("nextPageToken")
        if not nxt:
            return changes, str(d.get("newStartPageToken") or token), True
        token = str(nxt)
    return changes, token, False


# ---- Tasks API v1 ---------------------------------------------------------------------------------------
TASKS_API = "https://tasks.googleapis.com/tasks/v1/"
TASKLIST_PAGES = 20
TASK_PAGES = 100
TASK_PAGE_SIZE = 100                     # the API's maximum for tasks.list and tasklists.list
TASK_FIELDS = "nextPageToken,items(id,updated,status,due,deleted)"
TASK_FIELDS_WITH_TITLE = "nextPageToken,items(id,title,updated,status,due,deleted)"


class TasklistGone(Exception):
    """Tasks answered 404 for a task list (it was deleted between listing the lists and reading it)."""


def _tasks_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path


def tasks_lists(client, *, base: str = TASKS_API, max_pages: int = TASKLIST_PAGES) -> list[dict]:
    """The account's task lists, ``{id, title, updated}`` each, in the API's order (``tasklists.list``)."""
    out: list[dict] = []
    page = None
    for _ in range(max_pages):
        params: dict[str, Any] = {"maxResults": TASK_PAGE_SIZE, "fields": "nextPageToken,items(id,title,updated)"}
        if page:
            params["pageToken"] = page
        d = client.get(_tasks_url(base, "users/@me/lists"), params=params, label="tasks.lists")
        d = d if isinstance(d, dict) else {}
        out.extend(x for x in d.get("items") or [] if isinstance(x, dict) and x.get("id"))
        page = d.get("nextPageToken")
        if not page:
            break
    return out


def tasks_changes(client, tasklist_id: str, updated_min: str = "", *, base: str = TASKS_API, titles: bool = False,
                  max_pages: int = TASK_PAGES) -> tuple[list[dict], bool]:
    """(the tasks of one list modified at or after ``updated_min`` (all of them when it is empty), whether the read
    reached the end).

    Completed, hidden and deleted tasks are included: a task completed in Google's own apps is hidden, and a deleted
    one only shows with ``showDeleted``. Each task carries ``id``, ``updated``, ``status``, ``due``, ``deleted`` and,
    when ``titles`` is true, ``title``. Pages come in no useful order, so the caller sorts. Raises ``TasklistGone``
    when the list no longer exists (HTTP 404)."""
    fields = TASK_FIELDS_WITH_TITLE if titles else TASK_FIELDS
    url = _tasks_url(base, "lists/" + quote(str(tasklist_id), safe="") + "/tasks")
    tasks: list[dict] = []
    page = None
    for _ in range(max_pages):
        params: dict[str, Any] = {"maxResults": TASK_PAGE_SIZE, "showCompleted": "true", "showHidden": "true",
                                  "showDeleted": "true", "fields": fields}
        if updated_min:
            params["updatedMin"] = updated_min
        if page:
            params["pageToken"] = page
        d, status = _status_of(lambda: client.get(url, params=params, label="tasks.list"))
        if status == 404:
            raise TasklistGone()
        if status is not None:
            raise d
        d = d if isinstance(d, dict) else {}
        tasks.extend(x for x in d.get("items") or [] if isinstance(x, dict) and x.get("id"))
        page = d.get("nextPageToken")
        if not page:
            return tasks, True
    return tasks, False


# ---- Meet REST v2 ---------------------------------------------------------------------------------------
MEET_API = "https://meet.googleapis.com/v2/"
MEET_PAGE_SIZE = 250                     # the API's maximum for participants and sessions
PARTICIPANT_PAGES = 20
SESSION_PAGES = 5
SESSION_LOOKUPS = 100                    # participants whose sessions are listed; the rest keep their own times
MEET_RECORD_RE = re.compile(r"^conferenceRecords/[A-Za-z0-9_-]{1,128}$")
_MEET_PARTICIPANT_RE = re.compile(r"^conferenceRecords/[A-Za-z0-9_-]{1,128}/participants/[A-Za-z0-9_-]{1,128}$")


def _meet_url(base: str, name: str) -> str:
    return base.rstrip("/") + "/" + quote(name, safe="/")


def meet_conference(client, name: str, *, base: str = MEET_API) -> dict | None:
    """The conference record ``name`` (``conferenceRecords/<id>``): ``{name, startTime, endTime, expireTime, space}``,
    or None when Meet answers 404 (the record is gone or was never this account's)."""
    if not MEET_RECORD_RE.match(name or ""):
        raise Permanent("not a conference record name")
    rec, status = _status_of(lambda: client.get(_meet_url(base, name), label="meet.conference"))
    if status == 404:
        return None
    if status is not None:
        raise rec
    return rec if isinstance(rec, dict) else {}


def _meet_list(client, url: str, field: str, label: str, max_pages: int) -> tuple[list[dict], bool]:
    items: list[dict] = []
    page = None
    for _ in range(max_pages):
        params: dict[str, Any] = {"pageSize": MEET_PAGE_SIZE}
        if page:
            params["pageToken"] = page
        d = client.get(url, params=params, label=label)
        d = d if isinstance(d, dict) else {}
        items.extend(x for x in d.get(field) or [] if isinstance(x, dict))
        page = d.get("nextPageToken")
        if not page:
            return items, True
    return items, False


def meet_participants(client, record: str, *, base: str = MEET_API,
                      max_pages: int = PARTICIPANT_PAGES) -> tuple[list[dict], bool]:
    """(every participant of the conference record, whether the listing was read to its end)."""
    if not MEET_RECORD_RE.match(record or ""):
        raise Permanent("not a conference record name")
    return _meet_list(client, _meet_url(base, record + "/participants"), "participants", "meet.participants", max_pages)


def meet_sessions(client, participant: str, *, base: str = MEET_API,
                  max_pages: int = SESSION_PAGES) -> list[dict]:
    """Every session (``{name, startTime, endTime}``) of one participant (``conferenceRecords/<id>/participants/<id>``)."""
    if not _MEET_PARTICIPANT_RE.match(participant or ""):
        raise Permanent("not a participant name")
    return _meet_list(client, _meet_url(base, participant + "/participantSessions"), "participantSessions",
                      "meet.sessions", max_pages)[0]


def meet_participant(raw: dict, sessions: list[dict] | None = None) -> dict:
    """One participant as plain facts::

        {"display_name", "kind": "signedin" | "anonymous" | "phone" | "unknown",
         "user_id": the id of a signed-in user ("" otherwise), "first_join", "last_leave", "sessions": count or None}

    ``first_join`` / ``last_leave`` are the participant's own ``earliestStartTime`` / ``latestEndTime``, else the
    earliest start and latest end of ``sessions``; "" when neither says (``last_leave`` is "" while still in the call)."""
    kind, who = "unknown", {}
    for k, f in (("signedin", "signedinUser"), ("anonymous", "anonymousUser"), ("phone", "phoneUser")):
        if isinstance(raw.get(f), dict):
            kind, who = k, raw[f]
            break
    user = str(who.get("user") or "") if kind == "signedin" else ""
    starts = sorted(str(s["startTime"]) for s in sessions or [] if s.get("startTime"))
    ends = sorted(str(s["endTime"]) for s in sessions or [] if s.get("endTime"))
    return {"display_name": str(who.get("displayName") or ""), "kind": kind,
            "user_id": user.rpartition("/")[2] if user else "",
            "first_join": str(raw.get("earliestStartTime") or (starts[0] if starts else "")),
            "last_leave": str(raw.get("latestEndTime") or (ends[-1] if ends else "")),
            "sessions": None if sessions is None else len(sessions)}


def meet_attendance(client, record: str, *, base: str = MEET_API,
                    max_lookups: int = SESSION_LOOKUPS) -> tuple[list[dict], dict]:
    """(each participant of the conference record as ``meet_participant`` facts, in order of first join; stats).

    The sessions of the first ``max_lookups`` participants are listed; later ones keep their own times and carry
    ``sessions: None``. ``stats``: ``participants`` (count), ``lookups`` (session listings made) and ``truncated``
    (the participant listing hit its page cap)."""
    raw, complete = meet_participants(client, record, base=base)
    out, lookups = [], 0
    for p in raw:
        name = str(p.get("name") or "")
        sessions = None
        if lookups < max_lookups and _MEET_PARTICIPANT_RE.match(name):
            sessions = meet_sessions(client, name, base=base)
            lookups += 1
        out.append(meet_participant(p, sessions))
    out.sort(key=lambda x: (x["first_join"] == "", x["first_join"]))
    return out, {"participants": len(out), "lookups": lookups, "truncated": not complete}
