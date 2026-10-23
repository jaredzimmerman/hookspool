"""``http`` destination (contract C3; ARCHITECTURE §6.4, §14.2): any HTTPS endpoint that
accepts the Standard Webhooks document.

- ``https://`` is required unless ``allow_insecure = true``; TLS is always verified (system
  roots, or ``SSL_CERT_FILE`` for a private CA) and a certificate failure is a ``config``
  outcome (the operator fixes it), never a silent downgrade.
- Redirects are never followed: a 3xx is ``permanent`` (the URL is wrong for every attempt).
- ``secret`` is a secret reference, resolved on every attempt; ``secret_previous`` adds a
  second ``v1`` signature while a receiver rotates.
- Idempotency is per batch: ``webhook-id`` is identical on every attempt.
"""
from __future__ import annotations

from typing import ClassVar
from urllib.parse import urlsplit

from .. import config as _config
from .base import DeliveryContext, Destination, Outcome
from .wire import (RESERVED_HEADERS, BadSecret, NetError, classify_status, delivery_headers, header_value_ok,
                   net_outcome, post, signing_key)


class HttpDestination(Destination):
    type_name = "http"
    idempotency: ClassVar[str] = "per-batch"
    default_max_body_bytes = _config.HTTP_DEFAULT_MAX_BODY

    def __init__(self, name, spec, config):
        super().__init__(name, spec, config)
        self.url = str(self.spec.get("url") or "")
        self.allow_insecure = bool(self.spec.get("allow_insecure", False))
        self.extra_headers = {str(k): str(v) for k, v in (self.spec.get("headers") or {}).items()}

    def _tls_context(self):
        """Seam for tests that need a private CA without touching the environment."""
        return None

    def _url_problem(self) -> str | None:
        u = urlsplit(self.url)
        if u.scheme not in ("http", "https") or not u.hostname:
            return "url must be an http(s) URL"
        if u.scheme == "http" and not self.allow_insecure:
            return "url is http:// but allow_insecure is not set; https:// is required"
        return None

    def _secrets(self) -> list[str]:
        out = [_config.resolve_secret(self.spec.get("secret"))]
        if self.spec.get("secret_previous") is not None:
            out.append(_config.resolve_secret(self.spec["secret_previous"]))
        return out

    def deliver(self, doc: dict, body: bytes, delivery_id: str, ctx: DeliveryContext) -> Outcome:
        problem = self._url_problem()
        if problem:
            return Outcome.config(problem, detail=problem)
        try:
            secrets = self._secrets()
            headers = delivery_headers(secrets, delivery_id, ctx, body)
        except (_config.SecretError, BadSecret) as e:
            return Outcome.config(f"secret unusable: {e}", detail=f"secret unusable: {e}")
        for k, v in self.extra_headers.items():     # ours always win; one http.client would refuse is left out
            if k.lower() not in RESERVED_HEADERS and header_value_ok(k) and header_value_ok(v):
                headers.setdefault(k, v)
        budget = ctx.timeout(self.timeout_s)
        why = "deadline" if budget < self.timeout_s else "timeout"
        try:
            resp = post(self.url, headers, body, budget, ssl_context=self._tls_context())
        except NetError as e:
            return net_outcome(e, timed_out_reason=why)
        if 200 <= resp.status < 300:
            return Outcome.ok(f"http {resp.status}", status=resp.status)
        return classify_status(resp, ctx.now(), redirect_permanent=True)

    def check(self) -> list[str]:
        problems: list[str] = []
        p = self._url_problem()
        if p:
            problems.append(f"{self.name}: {p}")
        try:
            for s in self._secrets():
                signing_key(s)
        except (_config.SecretError, BadSecret) as e:
            problems.append(f"{self.name}: {e}")
        for k, v in self.extra_headers.items():
            if k.lower() in RESERVED_HEADERS:
                problems.append(f"{self.name}: header {k!r} is set by hookspool and cannot be overridden")
            if "\r" in k + v or "\n" in k + v:
                problems.append(f"{self.name}: header {k!r} contains a line break")
            elif not (header_value_ok(k) and header_value_ok(v)):
                problems.append(f"{self.name}: header {k!r} has characters that cannot be sent (control or"
                                " non-Latin-1); it is left out of every delivery")
        return problems
