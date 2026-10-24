"""A scripted stand-in for the Google endpoints the Gmail, Calendar, Drive, Meet and Tasks bobbins call (Calendar
events, Gmail history/messages/profile, free/busy, the Drive change feed, Meet conference records, participants and
sessions, Tasks lists and tasks, the OAuth token endpoint), on 127.0.0.1. Test values only."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit


class GoogleStub:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.calendar_lists: list = []          # popped per events.list: dict body, or int status
        self.created: list[dict] = []           # POSTed events
        self.deleted: list[str] = []
        self.profile = {"historyId": "100"}
        self.history: list = []                 # popped per history.list: dict body, or int status
        self.messages: dict[str, dict] = {}
        self.busy: list[dict] = []              # free/busy intervals {"start","end"}
        self.token_calls = 0
        self.fail_insert = False                # answer 500 to an event insert
        self.drive_start = "start-1"            # the answer to changes.getStartPageToken
        self.drive_pages: list = []             # popped per changes.list: dict body, or int status
        self.meet_records: dict = {}            # "conferenceRecords/<id>" -> record dict, or int status; absent = 404
        self.meet_participants: dict = {}       # record name -> list of page bodies (page N answers pageToken "pN"),
        #                                         or int status
        self.meet_sessions: dict = {}           # participant name -> list of sessions
        self.tasklists: list | int = []         # tasklists.list items, or an int status
        self.tasks: dict = {}                   # list id -> list of task dicts (id, updated, ...), or an int status
        self.tasks_page_size = 0                # when > 0, pages are this big whatever maxResults asks
        self.reject_bearers: set[str] = set()   # bearer tokens the Tasks routes answer 401 to
        stub = self

        class H(BaseHTTPRequestHandler):
            def _body(self) -> bytes:
                n = int(self.headers.get("Content-Length", 0))
                return self.rfile.read(n) if n else b""

            def _send(self, status: int, obj=None):
                data = b"" if obj is None else json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _route(self, method: str):
                u = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                body = self._body()
                with stub.lock:
                    stub.requests.append({"method": method, "path": u.path, "query": q, "body": body,
                                          "auth": self.headers.get("Authorization", "")})
                    p = u.path
                    if p == "/token" and method == "POST":
                        stub.token_calls += 1
                        return self._send(200, {"access_token": "tok-refreshed", "expires_in": 3600})
                    if p.startswith("/cal/events"):
                        if method == "GET":
                            nxt = stub.calendar_lists.pop(0) if stub.calendar_lists else {"items": [], "nextSyncToken": "s-same"}
                            return self._send(nxt, {"error": "scripted"}) if isinstance(nxt, int) else self._send(200, nxt)
                        if method == "POST":
                            if stub.fail_insert:
                                return self._send(500, {"error": "scripted"})
                            ev = json.loads(body or b"{}")
                            ev["id"] = f"created-{len(stub.created) + 1}"
                            stub.created.append(ev)
                            return self._send(200, ev)
                        if method == "DELETE":
                            stub.deleted.append(p.rsplit("/", 1)[-1])
                            return self._send(204)
                    if p.startswith("/tasks/v1/"):
                        if self.headers.get("Authorization", "").removeprefix("Bearer ") in stub.reject_bearers:
                            return self._send(401, {"error": "stale token"})
                        rest = unquote(p[len("/tasks/v1/"):])
                        if rest == "users/@me/lists":
                            if isinstance(stub.tasklists, int):
                                return self._send(stub.tasklists, {"error": "scripted"})
                            return self._send(200, {"items": stub.tasklists})
                        if rest.startswith("lists/") and rest.endswith("/tasks"):
                            rows = stub.tasks.get(rest[len("lists/"):-len("/tasks")], 404)
                            if isinstance(rows, int):
                                return self._send(rows, {"error": "scripted"})
                            keep = [t for t in rows
                                    if (q.get("showDeleted") == "true" or not t.get("deleted"))
                                    and (q.get("showHidden") == "true" or not t.get("hidden"))
                                    and (q.get("showCompleted") != "false" or t.get("status") != "completed")
                                    and (not q.get("updatedMin") or t.get("updated", "") >= q["updatedMin"])]
                            size = stub.tasks_page_size or int(q.get("maxResults", 20))
                            at = int(q["pageToken"][1:]) if q.get("pageToken") else 0
                            body = {"items": keep[at:at + size]}
                            if at + size < len(keep):
                                body["nextPageToken"] = f"p{at + size}"
                            return self._send(200, body)
                    if p == "/drive/changes/startPageToken":
                        return self._send(200, {"startPageToken": stub.drive_start}) if stub.drive_start \
                            else self._send(200, {})
                    if p == "/drive/changes":
                        nxt = stub.drive_pages.pop(0) if stub.drive_pages else {
                            "changes": [], "newStartPageToken": q.get("pageToken", "")}
                        return self._send(nxt, {"error": "scripted"}) if isinstance(nxt, int) else self._send(200, nxt)
                    if p.startswith("/meet/"):
                        name = p[len("/meet/"):]
                        if name.endswith("/participantSessions"):
                            part = name[:-len("/participantSessions")]
                            return self._send(200, {"participantSessions": stub.meet_sessions.get(part, [])})
                        if name.endswith("/participants"):
                            pages = stub.meet_participants.get(name[:-len("/participants")], [])
                            if isinstance(pages, int):
                                return self._send(pages, {"error": "scripted"})
                            i = int(q.get("pageToken", "p0")[1:])
                            return self._send(200, pages[i] if i < len(pages) else {})
                        rec = stub.meet_records.get(name, 404)
                        return self._send(rec, {"error": "scripted"}) if isinstance(rec, int) else self._send(200, rec)
                    if p == "/gmail/profile":
                        return self._send(200, stub.profile)
                    if p == "/gmail/history":
                        nxt = stub.history.pop(0) if stub.history else {"historyId": q.get("startHistoryId", "0")}
                        return self._send(nxt, {"error": "scripted"}) if isinstance(nxt, int) else self._send(200, nxt)
                    if p.startswith("/gmail/messages/"):
                        m = stub.messages.get(p.rsplit("/", 1)[-1])
                        return self._send(200, m) if m else self._send(404, {"error": "no such message"})
                    if p == "/freebusy" and method == "POST":
                        return self._send(200, {"calendars": {"primary": {"busy": stub.busy}}})
                self._send(404, {"error": "no route " + p})

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

            def do_DELETE(self):
                self._route("DELETE")

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def gmail_settings(self) -> dict[str, str]:
        """The gmail bobbin settings that point it at this stub."""
        return {"api_url": self.base + "/gmail/", "freebusy_url": self.base + "/freebusy"}

    def gcal_settings(self) -> dict[str, str]:
        """The gcal bobbin settings that point it at this stub."""
        return {"api_url": self.base + "/cal/events"}

    def drive_settings(self) -> dict[str, str]:
        """The drive bobbin settings that point it at this stub."""
        return {"api_url": self.base + "/drive/"}

    def tasks_settings(self) -> dict[str, str]:
        """The tasks bobbin settings that point it at this stub."""
        return {"api_url": self.base + "/tasks/v1/"}

    def meet_settings(self) -> dict[str, str]:
        """The meet bobbin settings that point it at this stub."""
        return {"api_url": self.base + "/meet/"}

    def count(self, method: str, prefix: str) -> int:
        return sum(1 for r in self.requests if r["method"] == method and r["path"].startswith(prefix))

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
