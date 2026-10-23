#!/usr/bin/env python3
"""GitHub redelivery sweep (docs/ARCHITECTURE.md section 13, point 5).

GitHub does not retry a webhook delivery that failed (a 5xx while the proxy or intake was down, a 429,
a timeout). This script lists the repository webhook's recent deliveries and asks GitHub to redeliver
the ones that never got a 2xx. It needs the ``gh`` CLI, authenticated with a token that may read and
redeliver the hook's deliveries (a fine-grained token with "Webhooks: read and write" on the repository).

    github_redeliver.py --repo OWNER/REPO --hook-id 123456 [--since-hours 48] [--max 50] [--dry-run]

Safe to run from cron once or twice a day, and safe to run twice in a row:

* a delivery GUID is redelivered only when NO attempt with that GUID got a 2xx (GitHub keeps the GUID
  across redeliveries, so a delivery that was redelivered and landed is not redelivered again);
* the hookspool github source dedups on ``X-GitHub-Delivery`` for ``dedup_window_s`` (24 h by default),
  so a redelivery of something that did land (a 2xx whose response GitHub never saw) is answered
  ``duplicate`` and not delivered twice, but ONLY while that delivery is inside the window. The default
  ``--since-hours`` (48) is longer than the default window: for a delivery older than the window the
  guard above (no attempt with a 2xx) is the only protection. Keep ``--since-hours`` at or below
  ``dedup_window_s`` if a double delivery matters more to you than a miss;
* ``ping`` deliveries are skipped, and so is anything older than ``--since-hours`` (GitHub keeps
  deliveries for three days, so 48 is the default). A delivery without a usable ``delivered_at`` or
  ``id`` is skipped and counted, never guessed at;
* a delivery whose newest attempt got **413** is not redelivered: it would be refused again until the
  body limit is raised end to end (deploy/README.md, Known limits). It is counted in the summary so the
  operator sees it.

Output is one summary line on stdout. Exit 0 when the sweep ran (even if some redeliveries were
refused: they are listed on stderr), 2 for bad arguments, 3 when ``gh`` itself failed or printed
something that is not the JSON the API documents.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone


class GhError(RuntimeError):
    pass


def gh(args: list[str], *, timeout: float = 60.0) -> str:
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GhError(f"gh {args[0]} {args[1] if len(args) > 1 else ''}: {e.__class__.__name__}") from None
    if p.returncode != 0:
        raise GhError(f"gh {' '.join(args[:2])} exited {p.returncode}: {p.stderr.strip()[:200]}")
    return p.stdout


def parse_pages(text: str) -> list[dict]:
    """``gh api --paginate`` prints one JSON array per page, back to back."""
    dec, i, out = json.JSONDecoder(), 0, []
    text = text.strip()
    while i < len(text):
        try:
            value, j = dec.raw_decode(text, i)
        except ValueError:
            raise GhError("gh api printed output that is not JSON") from None
        if isinstance(value, list):
            out.extend(d for d in value if isinstance(d, dict))
        i = j
        while i < len(text) and text[i].isspace():
            i += 1
    return out


def _ts(s: object) -> float | None:
    """Seconds since the epoch, or None when GitHub's timestamp is missing or unreadable."""
    if not isinstance(s, str):
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).timestamp()


def failed_deliveries(deliveries: list[dict], *, now: float, since_s: float,
                      skipped: list[dict] | None = None, too_large: list[dict] | None = None) -> list[dict]:
    """The newest attempt of every GUID that has no 2xx attempt, newest delivery first. Deliveries that
    cannot be judged (no usable ``delivered_at`` or ``id``) are left out and appended to ``skipped``; those
    whose newest attempt got 413 (a redelivery would get 413 again) are left out and appended to ``too_large``."""
    skipped = [] if skipped is None else skipped
    too_large = [] if too_large is None else too_large
    by_guid: dict[str, list[dict]] = {}
    for d in deliveries:
        by_guid.setdefault(str(d.get("guid") or d.get("id") or f"?{id(d)}"), []).append(d)
    out = []
    for attempts in by_guid.values():
        if any(isinstance(a.get("status_code"), int) and 200 <= a["status_code"] < 300 for a in attempts):
            continue
        dated = [(t, a) for a in attempts if (t := _ts(a.get("delivered_at"))) is not None]
        if not dated:
            skipped.append(attempts[0])                    # no usable timestamp: cannot judge its age
            continue
        t, newest = max(dated, key=lambda p: p[0])
        if newest.get("event") == "ping" or now - t > since_s:
            continue
        if not isinstance(newest.get("id"), int):
            skipped.append(newest)                         # nothing to redeliver by
            continue
        if newest.get("status_code") == 413:
            too_large.append(newest)                       # same body, same limit: it would 413 again
            continue
        out.append((t, newest))
    return [a for _t, a in sorted(out, key=lambda p: p[0], reverse=True)]


def main(argv: list[str] | None = None, *, now: float | None = None) -> int:
    ap = argparse.ArgumentParser(description="Redeliver GitHub webhook deliveries that never got a 2xx.")
    ap.add_argument("--repo", required=True, metavar="OWNER/REPO")
    ap.add_argument("--hook-id", required=True, type=int)
    ap.add_argument("--since-hours", type=float, default=48.0,
                    help="ignore deliveries older than this (default 48; the source's dedup window is 24 h by "
                         "default, so beyond it an already-landed delivery would not be answered duplicate)")
    ap.add_argument("--max", type=int, default=50, help="redeliveries per run (default 50)")
    ap.add_argument("--dry-run", action="store_true", help="list what would be redelivered")
    a = ap.parse_args(argv)
    if a.repo.count("/") != 1 or not all(a.repo.split("/")):
        ap.error("--repo must be OWNER/REPO")
    base = f"repos/{a.repo}/hooks/{a.hook_id}/deliveries"
    now = time.time() if now is None else now
    try:
        deliveries = parse_pages(gh(["api", "--paginate", f"{base}?per_page=100"]))
        unusable: list[dict] = []
        too_large: list[dict] = []
        todo = failed_deliveries(deliveries, now=now, since_s=a.since_hours * 3600, skipped=unusable,
                                 too_large=too_large)
        sent = refused = 0
        for d in todo[: max(0, a.max)]:
            if a.dry_run:
                print(f"would redeliver {d.get('guid')} id={d['id']} event={d.get('event')} status={d.get('status_code')}")
                sent += 1
                continue
            try:
                gh(["api", "-X", "POST", f"{base}/{d['id']}/attempts"])
                sent += 1
            except GhError as e:
                refused += 1
                print(f"refused {d.get('guid')}: {e}", file=sys.stderr)
    except GhError as e:
        print(f"sweep failed: {e}", file=sys.stderr)
        return 3
    left = max(0, len(todo) - a.max)
    extra = f", {len(unusable)} skipped (no usable delivered_at or id)" if unusable else ""
    if too_large:
        extra += f", {len(too_large)} not redelivered (413: raise the body limit, deploy/README.md Known limits)"
    print(f"{a.repo} hook {a.hook_id}: {len(deliveries)} deliveries, {len(todo)} without a 2xx, "
          f"{sent} {'listed' if a.dry_run else 'redelivered'}, {refused} refused, {left} left for the next run{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
