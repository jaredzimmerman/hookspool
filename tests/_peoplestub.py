"""A scripted stand-in for the Google People API endpoints the ``google.contacts`` bobbin calls
(``people/me/connections``, ``otherContacts`` and the OAuth token endpoint), on 127.0.0.1. Test values only.

Each list pops its scripted answers in order: a dict is a 200 body, an int is that status with an error body. With
nothing scripted a list answers an empty page and a fresh sync token."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class PeopleStub:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.connections: list = []
        self.other: list = []
        self.token_calls = 0
        stub = self

        class H(BaseHTTPRequestHandler):
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
                n = int(self.headers.get("Content-Length", 0))
                if n:
                    self.rfile.read(n)
                with stub.lock:
                    stub.requests.append({"method": method, "path": u.path, "query": q,
                                          "auth": self.headers.get("Authorization", "")})
                    if u.path == "/token" and method == "POST":
                        stub.token_calls += 1
                        return self._send(200, {"access_token": "tok-refreshed", "expires_in": 3600})
                    queue = {"/v1/people/me/connections": stub.connections, "/v1/otherContacts": stub.other}.get(u.path)
                    if queue is None or method != "GET":
                        return self._send(404, {"error": "no route " + u.path})
                    nxt = queue.pop(0) if queue else {"nextSyncToken": "tok-empty"}
                    if isinstance(nxt, int):
                        return self._send(nxt, {"error": {"code": nxt, "status": "SCRIPTED",
                                                          "message": "scripted"}})
                    return self._send(200, nxt)

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

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

    def settings(self) -> dict[str, str]:
        """The google.contacts settings that point it at this stub."""
        return {"api_url": self.base + "/v1/"}

    def calls(self, path: str) -> list[dict]:
        return [r for r in self.requests if r["path"] == path]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
