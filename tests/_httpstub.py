"""A scriptable HTTP(S) receiver for destination tests, plus an independent Standard
Webhooks verifier (written from the specification, sharing no code with hookspool)."""
from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import ipaddress
import json
import os
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hookspool.destinations.base import DeliveryContext


def make_ctx(*, batch_id: int = 7, attempt: int = 1, attempt_id: int = 11, delivery_id: str = "hs_x_7",
             now: float = 1_800_000_000.0, ttl: float = 60.0, wall: bool = False, probe: bool = False):
    """A DeliveryContext. ``wall=True`` uses the real clock (for tests that really wait)."""
    clock = time.time if wall else (lambda: now)
    t0 = clock()
    return DeliveryContext(batch_id=batch_id, attempt=attempt, attempt_id=attempt_id, delivery_id=delivery_id,
                           probe=probe, lease_until=t0 + ttl + 5, deadline=t0 + ttl, now=clock)


def verify_standard_webhooks(headers, body: bytes, secret: str, *, now: float, tolerance: float = 300.0) -> bool:
    """Standard Webhooks verification, straight from the spec: key is the base64-decoded
    remainder of a ``whsec_`` secret (else the raw bytes), content is ``id.timestamp.body``,
    the header holds space-separated ``v1,<b64>`` entries."""
    h = {k.lower(): v for k, v in headers.items()}
    msg_id, ts, sigs = h.get("webhook-id"), h.get("webhook-timestamp"), h.get("webhook-signature")
    if not (msg_id and ts and sigs):
        return False
    if abs(now - int(ts)) > tolerance:
        return False
    key = base64.b64decode(secret[6:]) if secret.startswith("whsec_") else secret.encode()
    want = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return any(hmac.compare_digest(p.split(",", 1)[1], want) for p in sigs.split(" ")
               if p.startswith("v1,"))


def verify_legacy(headers, body: bytes, secret: str) -> bool:
    got = {k.lower(): v for k, v in headers.items()}.get("x-hub-signature-256", "")
    want = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(got, want)


class Stub:
    """Records requests; ``script`` is a list of response specs popped per request:
    ``{"status": 200, "headers": {...}, "body": b"...", "delay": s, "drip": s, "hold": Event}``.
    ``default`` answers when the script is empty."""

    def __init__(self, *, tls: tuple[str, str] | None = None):
        self.requests: list[dict] = []
        self.script: list[dict] = []
        self.default = {"status": 200, "body": b'{"status":"accepted"}'}
        self.lock = threading.Lock()
        stub = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n)
                with stub.lock:
                    spec = stub.script.pop(0) if stub.script else stub.default
                    stub.requests.append({"path": self.path, "headers": {k: v for k, v in self.headers.items()},
                                          "body": body, "t": time.monotonic()})
                if spec.get("hold") is not None:
                    spec["hold"].wait(30)
                if spec.get("delay"):
                    time.sleep(spec["delay"])
                try:
                    self.send_response(spec["status"])
                    for k, v in (spec.get("headers") or {}).items():
                        self.send_header(k, v)
                    payload = spec.get("body", b"")
                    if spec.get("drip"):
                        self.send_header("Content-Length", str(len(payload) + 100))
                        self.end_headers()
                        for _ in range(100):
                            self.wfile.write(b"x")
                            self.wfile.flush()
                            time.sleep(spec["drip"])
                        return
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                    pass

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*tls)
            self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self.scheme = "https" if tls else "http"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"{self.scheme}://127.0.0.1:{self.port}"

    def respond(self, *specs: dict) -> "Stub":
        self.script.extend(specs)
        return self

    def close(self) -> None:
        for s in self.script:
            if s.get("hold") is not None:
                s["hold"].set()
        self.server.shutdown()
        self.server.server_close()


def make_cert(directory, *, name: str = "srv", ip: str = "127.0.0.1", dns: str | None = None):
    """Write a self-signed certificate (valid for ``ip``/``dns``) and return (cert_path, key_path)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "hookspool test")])
    san = [x509.IPAddress(ipaddress.ip_address(ip))] + ([x509.DNSName(dns)] if dns else [])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=2))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    cert_path = os.path.join(str(directory), f"{name}.pem")
    key_path = os.path.join(str(directory), f"{name}.key")
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return cert_path, key_path


def doc_for(count: int = 2, source: str = "github", event_type: str = "push") -> dict:
    """A §6.4 document with a payload sentinel, for destination tests."""
    return {"source": source, "route": "r", "key": "k", "batch_id": 7, "delivery_id": "hs_x_7", "attempt": 1,
            "count": count, "first_at": 1.0, "last_at": 2.0, "event_type": event_type,
            "event_types": {event_type: count}, "summary": f"{count} {source} event(s) for k",
            "content_hash": "sha256:abc", "events": [{"n": i, "note": "PAYLOAD-SENTINEL-7f3a"} for i in range(count)],
            "event_meta": [{"id": i} for i in range(count)]}


def encode(doc: dict) -> bytes:
    return json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
