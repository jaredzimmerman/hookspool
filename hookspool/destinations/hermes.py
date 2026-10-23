"""``hermes:<route>`` destination (contract C3; ARCHITECTURE §6.4, §6.5, §14.2).

POSTs the batch document to the Hermes webhook platform:

    <base_url>/p/<profile>/webhooks/<route>      (the route names a profile)
    <base_url>/webhooks/<route>                  (otherwise)

The route's secret and profile are read from ``hermes.subscriptions_path`` on every attempt,
so a rotation or a profile change needs no restart. Idempotency is per attempt (a fresh
``webhook-id`` each time): Hermes records a delivery id on admission and answers
``duplicate`` afterwards, which proves admission, not processing (§6.4).

With ``unwrap = true`` the batch goes out as one request per event, each carrying the event's
stored payload exactly as the sender's original body, so a gateway route with payload prompt
templates sees what it would have seen without hookspool. Idempotency is then per event
(``hs_<instance>_<batch>_<event id>`` on every attempt), so an event the gateway already admitted
is answered ``duplicate`` when a later failure in the same batch makes it retry; ``ignored`` and
``coalesced`` are the gateway's own filter and coalescing at work and add no ``notice``.

Outcomes: 2xx is ``ok`` (a body ``status`` of ``ignored``/``coalesced`` adds a ``notice``);
401, 403 and 404 are ``config`` with a pointed reason; 400, 413, 415, 422 are ``permanent``;
429 and 503 with ``Retry-After`` are ``throttle``; connection failures, timeouts and other 5xx
are ``transport``.
"""
from __future__ import annotations

import json
import os
from typing import ClassVar
from urllib.parse import quote, urlsplit

from .. import config as _config
from .base import DeliveryContext, Destination, Outcome
from .wire import (BadSecret, NetError, Response, classify_status, delivery_headers, event_extra_headers,
                   header_name_ok, header_value_ok, net_outcome, post, signing_key, unwrap_deliver)

NOTICE_STATUSES = ("ignored", "coalesced")


def event_header_ok(name) -> bool:
    """A source's ``event_header`` may name any plain header except one hookspool sets."""
    return header_name_ok(name)


class HermesDestination(Destination):
    type_name = "hermes"
    idempotency: ClassVar[str] = "per-attempt"

    def __init__(self, name, spec, config):
        super().__init__(name, spec, config)
        self.route = name.split(":", 1)[1] if name.startswith("hermes:") else ""
        h = config.get("hermes") or {}
        self.base_url = str(h.get("base_url") or "").rstrip("/")
        self.subscriptions_path = os.path.expanduser(str(h.get("subscriptions_path") or ""))
        self.legacy_signature = bool(h.get("legacy_signature", True))
        self.unwrap = self.spec.get("unwrap") is True
        if self.unwrap:
            self.idempotency = "per-event"
        eh, dh = self.spec.get("event_header"), self.spec.get("dedup_header")
        self.event_header = eh if header_name_ok(eh) else None
        self.dedup_header = dh if header_name_ok(dh) else None

    def _default_timeout(self, config: dict) -> float:
        return float((config.get("hermes") or {}).get("timeout_s") or self.default_timeout_s)

    # ---- the route's secret and profile, read fresh --------------------------------------------
    def _route(self) -> tuple[str, str | None]:
        """(secret, profile). Raises ``LookupError`` with a safe message."""
        if not self.route:
            raise LookupError(f"destination {self.name!r} names no route (use hermes:<route>)")
        try:
            subs = json.loads(_config.read_regular_text(self.subscriptions_path, "utf-8-sig"))
        except OSError as e:
            raise LookupError(f"subscriptions file unreadable ({e.__class__.__name__})") from None
        except ValueError:      # includes UnicodeDecodeError
            raise LookupError("subscriptions file is not valid JSON") from None
        r = subs.get(self.route) if isinstance(subs, dict) else None
        if not isinstance(r, dict):
            raise LookupError(f"unknown route {self.route!r} in the subscriptions file")
        secret = r.get("secret")
        if not isinstance(secret, str) or not secret.strip():
            raise LookupError(f"route {self.route!r} has no secret in the subscriptions file")
        profile = r.get("profile")
        if profile is not None and not (isinstance(profile, str) and profile.strip()):
            raise LookupError(f"route {self.route!r} has an invalid profile")
        return secret, (profile.strip() if profile else None)

    def _event_header(self, doc: dict) -> str | None:
        """The header that carries the event type, if any: the destination's own ``event_header``
        (unwrap), else the source's ``event_header`` setting (any source, a template source
        included, can ask for one; the GitHub example sets ``X-GitHub-Event``, which Hermes'
        GitHub route reads). A name that is not a plain token, or that is one of the headers
        hookspool sets itself, is never used."""
        if self.unwrap and self.event_header:
            return self.event_header
        src = str(doc.get("source") or "")
        spec = (self.config.get("sources") or {}).get(src)
        h = spec.get("event_header") if isinstance(spec, dict) else None
        return h if event_header_ok(h) else None

    def url(self, profile: str | None) -> str:
        path = f"/webhooks/{quote(self.route, safe='')}"
        if profile:
            path = f"/p/{quote(profile, safe='')}{path}"
        return self.base_url + path

    # ---- delivery ------------------------------------------------------------------------------
    def deliver(self, doc: dict, body: bytes, delivery_id: str, ctx: DeliveryContext) -> Outcome:
        try:
            secret, profile = self._route()
        except LookupError as e:
            return Outcome.config(str(e), detail=str(e))
        if not self.base_url:
            return Outcome.config("hermes.base_url is not set", detail="hermes.base_url is not set")
        try:
            signing_key(secret)
        except BadSecret as e:
            return Outcome.config(f"route {self.route!r} secret unusable: {e}", detail=str(e))
        if self.unwrap:
            return self._deliver_unwrapped(doc, delivery_id, ctx, secret, profile)
        try:
            headers = delivery_headers([secret], delivery_id, ctx, body, legacy=self.legacy_signature)
        except BadSecret as e:
            return Outcome.config(f"route {self.route!r} secret unusable: {e}", detail=str(e))
        hdr = self._event_header(doc)
        value = str(doc.get("event_type") or "")
        if hdr and value and header_value_ok(value):     # a value http.client would refuse is left out
            headers[hdr] = value
        budget = ctx.timeout(self.timeout_s)
        why = "deadline" if budget < self.timeout_s else "timeout"
        try:
            resp = post(self.url(profile), headers, body, budget)
        except NetError as e:
            return net_outcome(e, timed_out_reason=why)
        return self._outcome(resp, ctx)

    def _deliver_unwrapped(self, doc: dict, delivery_id: str, ctx: DeliveryContext, secret: str,
                           profile: str | None) -> Outcome:
        """One request per event: the stored payload as the body, signed per event (Standard
        Webhooks and ``X-Hub-Signature-256``), with the event's type and dedup id as headers."""
        url = self.url(profile)
        ev_header = self._event_header(doc)

        def send_one(meta: dict, body: bytes, event_id: str) -> Outcome:
            headers = delivery_headers([secret], event_id, ctx, body, legacy=True)
            for k, v in event_extra_headers(meta, ev_header, self.dedup_header).items():
                headers.setdefault(k, v)
            budget = ctx.timeout(self.timeout_s)
            why = "deadline" if budget < self.timeout_s else "timeout"
            try:
                resp = post(url, headers, body, budget)
            except NetError as e:
                return net_outcome(e, timed_out_reason=why)
            return self._outcome(resp, ctx)
        return unwrap_deliver(doc, delivery_id, ctx, send_one)

    def _outcome(self, resp: Response, ctx: DeliveryContext) -> Outcome:
        code = resp.status
        if 200 <= code < 300:
            status = ""
            try:
                parsed = json.loads(resp.body or b"{}")
                if isinstance(parsed, dict) and isinstance(parsed.get("status"), str):
                    status = parsed["status"]
            except ValueError:
                pass
            # unwrap: the gateway sees original events, so its own filter and coalescing are expected
            notice = status if status in NOTICE_STATUSES and not self.unwrap else None
            reason = f"http {code}" + (f" {status}" if status else "")
            return Outcome.ok(reason, status=code, notice=notice)
        if code == 401:
            return Outcome.config("bad secret: Hermes rejected the signature (http 401)", status=code,
                                  detail=f"http 401: route {self.route!r} signature rejected; check the secret")
        if code == 403:
            return Outcome.config("forbidden: the route is not usable by this sender (http 403)", status=code,
                                  detail=f"http 403: route {self.route!r} refused (missing secret or profile)")
        if code == 404:
            return Outcome.config(f"unknown route {self.route!r} (http 404)", status=code,
                                  detail=f"http 404: Hermes has no route {self.route!r} here")
        if code == 413:
            detail = ("http 413: the event exceeds Hermes' max_body_bytes; exclude the event or raise"
                      " max_body_bytes" if self.unwrap else
                      "http 413: the document exceeds Hermes' max_body_bytes; split the batch")
            return Outcome.permanent("http 413 payload too large for Hermes", status=code, detail=detail)
        return classify_status(resp, ctx.now(), redirect_permanent=False)

    # ---- check ---------------------------------------------------------------------------------
    def check(self) -> list[str]:
        problems: list[str] = []
        u = urlsplit(self.base_url)
        if u.scheme not in ("http", "https") or not u.hostname:
            problems.append(f"{self.name}: hermes.base_url must be an http(s) URL")
        try:
            secret, _ = self._route()
        except LookupError as e:
            problems.append(f"{self.name}: {e}")
        else:
            try:
                signing_key(secret)
            except BadSecret as e:
                problems.append(f"{self.name}: route secret unusable: {e}")
        return problems
