"""Run one batch through a bobbin's processor for the conformance kit (a child process).

``hookspool.testing.bobbinkit`` starts this with the environment the dispatcher would give the
processor (14.3.3) and a spec file naming the stub HTTP answers and the mode. It runs the real
runtime (``hookspool.processor.run``) with the network blocked, ``ctx.http`` answered by
``StubHttp`` and standard output carrying only what the runtime prints, exactly as
``python -m hookspool.processor`` does. It writes a small report (event types returned, process
calls, HTTP calls by host, blocked network attempts) next to the spec and exits with the
runtime's code.

Modes: ``normal``; ``raise`` (an exception inside the batch: the stub HTTP raises ``Retryable`` on
the first call when the processor calls the API, otherwise ``process`` raises); ``permanent``
(the same with ``Permanent``, or ``ctx.fail_permanent``).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
from typing import Any


class StubHttp:
    """Answers ``ctx.http`` calls from the kit's ``[[http]]`` stubs (method + URL prefix); an
    unmatched call gets ``{}``. A stub with a non-2xx status raises what ``net.Http`` would.
    ``get_text`` answers from a stub's ``text`` (or its ``json``, serialized) and ``headers``, and a 304
    stub is ``(304, "", headers)``, as ``net.Http.get_text`` returns it; unmatched, it is ``(200, "", {})``."""

    def __init__(self, stubs: list[dict], report: dict, fault: str | None = None):
        self.stubs = stubs
        self.report = report
        self.fault = fault

    def get(self, url: str, **kw: Any) -> Any:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> Any:
        return self.request("POST", url, **kw)

    def _match(self, method: str, url: str) -> tuple[str, dict | None]:
        from hookspool import net
        method = str(method).upper()
        host = urllib.parse.urlsplit(str(url)).hostname or "?"
        self.report["http_calls"] += 1
        self.report["http_hosts"] = sorted(set(self.report["http_hosts"]) | {host})
        if self.fault == "raise":
            raise net.Retryable(f"{method} {host}: bobbin kit: injected outage")
        if self.fault == "permanent":
            raise net.Permanent(f"{method} {host}: bobbin kit: injected permanent error")
        base = str(url).split("?", 1)[0]
        for s in self.stubs:
            if str(s.get("method", "GET")).upper() == method and base.startswith(str(s.get("url", ""))):
                return host, s
        return host, None

    def request(self, method: str, url: str, *, json: Any = None, form: dict | None = None,
                params: dict | None = None, headers: dict | None = None, bearer: str | None = None,
                timeout_s: float | None = None, label: str | None = None) -> Any:
        from hookspool import net
        host, s = self._match(method, url)
        if s is None:
            return {}
        status = int(s.get("status", 200))
        if not 200 <= status < 300:
            cls = net.classify_status(status) or net.Permanent
            raise cls(f"{str(method).upper()} {host}: HTTP {status}", status=status)
        return s.get("json", {})

    def get_text(self, url: str, headers: dict | None = None, *, params: dict | None = None,
                 bearer: str | None = None, timeout_s: float | None = None,
                 label: str | None = None) -> tuple[int, str, dict]:
        from hookspool import net
        host, s = self._match("GET", url)
        if s is None:
            return 200, "", {}
        status = int(s.get("status", 200))
        hdrs = {str(k).lower(): str(v) for k, v in (s.get("headers") or {}).items()}
        if status == 304:
            return 304, "", hdrs
        if not 200 <= status < 300:
            cls = net.classify_status(status) or net.Permanent
            raise cls(f"GET {host}: HTTP {status}", status=status)
        text = s["text"] if isinstance(s.get("text"), str) else (json.dumps(s["json"]) if "json" in s else "")
        return status, text, hdrs


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        sys.stderr.write("usage: _procrun <spec.json>\n")
        return 75
    with open(argv[0], encoding="utf-8") as f:
        spec = json.load(f)
    from hookspool import net
    from hookspool import processor as P
    from hookspool.testing.netblock import block_network

    instance, mode = spec["instance"], spec.get("mode", "normal")
    via_http = bool(spec.get("fault_via_http"))
    report: dict[str, Any] = {"process_calls": 0, "event_types": [], "items": 0, "http_calls": 0, "http_hosts": [],
                              "network": []}
    stub = StubHttp(list(spec.get("http") or []), report, fault=mode if via_http and mode != "normal" else None)
    now = float(spec.get("now") or 1_800_000_000.0)

    def clock() -> float:
        return now

    def log(line: str) -> None:
        sys.stderr.write(f"hookspool.processor {instance}: {' '.join(str(line).split())[:1000]}\n")

    def extras(state: dict, resolver):
        return net.make_context_extras(state, resolver, http=stub, now=clock, log=log)

    real_load = P.load_processor

    class _Wrapped:
        def __init__(self, mod):
            self._mod = mod

        def _call(self, func, batch, ctx):
            report["process_calls"] += 1
            if mode == "raise" and not via_http:
                raise RuntimeError("bobbin kit: injected failure")
            if mode == "permanent" and not via_http:
                ctx.fail_permanent("bobbin kit: injected permanent failure")
            items = getattr(self._mod, func)(batch, ctx)
            if isinstance(items, list):
                report["items"] = len(items)
                report["event_types"] = sorted({str(i.get("event_type")) for i in items if isinstance(i, dict)})
            return items

        def process(self, batch, ctx):
            return self._call("process", batch, ctx)

        def poll(self, batch, ctx):                 # a poll bobbin's poller.py (14.4)
            return self._call("poll", batch, ctx)

    def load_processor(*a, **kw):
        return _Wrapped(real_load(*a, **kw))

    P.load_processor = load_processor
    sys.stdout.flush()
    real = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    saved, sys.stdout = sys.stdout, sys.stderr
    try:
        with block_network(loopback=False) as attempts:      # any real socket fails the check, loopback included
            code = P.run(instance, stdin=sys.stdin, out=real, err=sys.stderr, clock=clock, extras_factory=extras)
        report["network"] = list(attempts)
    finally:
        sys.stdout = saved
        try:
            real.close()
        except OSError:
            pass
    with open(spec["report"], "w", encoding="utf-8") as f:
        json.dump(report, f)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
