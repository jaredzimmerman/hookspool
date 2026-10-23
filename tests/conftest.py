import json
import logging
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hookspool import config as hs_config  # noqa: E402
from hookspool.store import Store  # noqa: E402

# The dispatcher rig fixture (tests/_dispatchkit.py), registered here so test modules
# need not import a name the fixture parameter would shadow.
pytest_plugins = ["_dispatchkit"]

# Every test payload carries this marker; no hookspool log line may ever contain it.
SENTINEL = "PAYLOAD-SENTINEL-7f3a"
T0 = 1_800_000_000.0


class FakeClock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> float:
        self.t += s
        return self.t


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HOOKSPOOL_ALERT_DRY", "1")
    monkeypatch.setenv("HOOKSPOOL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOOKSPOOL_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setenv("HOOKSPOOL_DB", str(tmp_path / "default-spool.db"))
    monkeypatch.delenv("HOOKSPOOL_LOG", raising=False)
    for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(k, raising=False)


# Secret values the tests use; none may reach a log line either (ARCHITECTURE §10).
KNOWN_SECRETS = ("test-secret-A", "test-secret-plain", "right-secret", "old-secret", "new-secret",
                 "rotated-secret-B", "audit-secret-1", "plain-secret", "zoom-secret", "other-secret",
                 "demo-secret", "wrong-secret")


def rendered_log_text(records) -> str:
    """Everything a hookspool log line can carry: the message, every structured field, both
    service formatters' output, and an exception's text (which the formatters drop, but a
    deployment's own handler might not)."""
    from hookspool import logs as hs_logs
    fmts = (hs_logs.TextFormatter(), hs_logs.JsonFormatter())
    out = []
    for r in records:
        out.append(r.getMessage())
        out.extend(str(v) for k, v in r.__dict__.items() if k in hs_logs.FIELDS and v is not None)
        out.extend(f.format(r) for f in fmts)
        if r.exc_info and r.exc_info[1] is not None:
            out.append(repr(r.exc_info[1]))
    return "\n".join(out)


@pytest.fixture(autouse=True)
def no_payload_in_logs(caplog):
    """The log sentinel, across the whole suite: no payload marker and no test secret in any
    hookspool log record, rendered the way the services render it."""
    caplog.set_level(logging.DEBUG, logger="hookspool")
    yield
    # caplog.records is the *current* phase only (teardown, here): the test's own records are in
    # the "setup" and "call" phases, which the first version of this fixture never looked at.
    records = caplog.get_records("setup") + caplog.get_records("call") + caplog.records
    text = rendered_log_text(records)
    assert SENTINEL not in text, "a hookspool log line contains a payload body"
    leaked = [s for s in KNOWN_SECRETS if s in text]
    assert not leaked, f"a hookspool log line contains a test secret ({len(leaked)} value(s))"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def subs_path(tmp_path):
    p = tmp_path / "webhook_subscriptions.json"
    write_subs(p, {"test-route": {"secret": "test-secret-A", "profile": "test-profile", "events": []},
                   "plain-route": {"secret": "test-secret-plain", "events": []}})
    return p


def write_subs(path, data):
    tmp = str(path) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


@pytest.fixture
def make_config(tmp_path, subs_path):
    """A v2 config dict (DEFAULTS merged with test values, then ``over``)."""
    def make(port: int = 9, **over):
        cfg = hs_config.merge(hs_config.DEFAULTS, {
            "spool": {"home": str(tmp_path / "home")},
            "hermes": {"base_url": f"http://127.0.0.1:{port}", "subscriptions_path": str(subs_path),
                       "timeout_s": 5},
            "alerts": {"dry": True},
        })
        return hs_config.merge(cfg, over)
    return make


@pytest.fixture
def make_store(tmp_path, clock):
    """Store on a fresh migrated spool; ``durable=False`` only where a test asks for speed."""
    made = []

    def make(cfg: dict | None = None, *, path=None, durable: bool = True, now=None) -> Store:
        st = Store(str(path or tmp_path / "spool.db"), cfg if cfg is not None else hs_config.merge(
            hs_config.DEFAULTS, {}), now=now or clock, durable=durable)
        st.ensure_schema()
        made.append(st)
        return st
    yield make
    for st in made:
        st.close()


class HermesStub:
    """Records POSTs. `responses` is a list of status codes popped per request (default 200)."""

    def __init__(self, port: int = 0):
        self.requests = []
        self.responses = []
        self.delay = 0.0
        self.hold = None           # threading.Event: when set to an unset Event, requests block on it
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()
        stub = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n)
                with stub._lock:
                    stub.active += 1
                    stub.max_active = max(stub.max_active, stub.active)
                    code = stub.responses.pop(0) if stub.responses else 200
                    rec = {"path": self.path, "headers": self.headers, "body": body,
                           "start": time.monotonic()}
                    stub.requests.append(rec)
                try:
                    if stub.hold is not None:
                        stub.hold.wait(30)
                    if stub.delay:
                        time.sleep(stub.delay)
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status":"accepted"}')
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    with stub._lock:
                        stub.active -= 1
                        rec["end"] = time.monotonic()

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def bodies(self):
        return [json.loads(r["body"]) for r in self.requests]

    def close(self):
        if self.hold is not None:
            self.hold.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def hermes():
    stubs = []

    def start(port: int = 0) -> HermesStub:
        s = HermesStub(port)
        stubs.append(s)
        return s
    yield start
    for s in stubs:
        s.close()


def child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return env
