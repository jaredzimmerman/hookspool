#!/usr/bin/env python3
"""Label a pull request and post its bobbin scorecard (stdlib only; run by .github/workflows/bobbin-label.yml).

The pull request job (bobbin.yml) has a read-only token, so a fork's code never holds a token that can
write. It uploads its scorecards as an artifact; this script runs afterwards in the base repository, from
the default branch, with a token that may label and comment. It never runs the pull request's code and
treats the artifact as untrusted data:

* the pull request number comes from the artifact, so it is used only after the GitHub API confirms that
  the pull request's head commit is the head commit of the run that produced the artifact;
* only known fields are read, every value is checked (bobbin names, check names, results, the scope), and
  every piece of text is shortened and escaped before it goes into the comment, so the comment cannot
  mention people, add links or break out of its table;
* the labels are only ``bobbin-only`` (every changed bobbin touches nothing outside itself) and
  ``touches-core`` (one does), one replacing the other;
* one comment, found by its hidden marker, is created or updated, never a second one.

    python .github/scripts/bobbin_label.py --reports DIR --repo OWNER/NAME --head-sha SHA [--dry-run]

``GH_TOKEN`` holds the token; ``GITHUB_API_URL`` the API root (default https://api.github.com).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Callable

SCHEMA = "hookspool.bobbin-check/1"
MARKER = "<!-- hookspool-bobbin-scorecard -->"
LABELS = ("bobbin-only", "touches-core")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
CHECK_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
RESULTS = ("pass", "fail", "skip")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MAX_TEXT = 300
MAX_BOBBINS = 20
MAX_CHECKS = 20

Api = Callable[[str, str, Any], Any]          # (method, path, json body or None) -> parsed JSON


class Refused(Exception):
    """The artifact cannot be trusted or used; nothing is labelled."""


def safe(text: Any, limit: int = MAX_TEXT) -> str:
    """Untrusted text for a table cell: one line, at most ``limit`` characters, no HTML, no mention, no link,
    no table or code break-out."""
    s = " ".join(str(text).split())[:limit]
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace("@", "&#64;").replace("|", "\\|").replace("`", "'")
    s = s.replace("[", "&#91;").replace("]", "&#93;")
    s = re.sub(r"(?i)\b(https?|ftp|wss?)://", r"\1&#58;//", s)
    return s


def read_reports(report_dir: str) -> tuple[int | None, list[dict], list[str]]:
    """(pr number from meta.json, validated scorecards, the valid bobbin names meta.json says were checked)."""
    try:
        with open(os.path.join(report_dir, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        raise Refused("no meta.json in the artifact") from None
    pr = meta.get("pr") if isinstance(meta, dict) else None
    if pr is not None and (not isinstance(pr, int) or isinstance(pr, bool) or not 0 < pr < 10 ** 9):
        raise Refused("meta.json: pr is not a pull request number")
    listed = meta.get("bobbins") if isinstance(meta, dict) else None
    names = [n for n in (listed if isinstance(listed, list) else [])[:MAX_BOBBINS]
             if isinstance(n, str) and NAME_RE.match(n)]
    cards = []
    for p in sorted(glob.glob(os.path.join(report_dir, "*.json")))[:MAX_BOBBINS + 1]:
        if os.path.basename(p) == "meta.json":
            continue
        try:
            with open(p, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            raise Refused(f"{os.path.basename(p)} is not JSON") from None
        cards.append(validate(doc))
    return pr, cards[:MAX_BOBBINS], names


def validate(doc: Any) -> dict:
    """The fields this script uses, checked; anything else in the document is ignored."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise Refused("a report is not a bobbin scorecard")
    name = doc.get("bobbin")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise Refused("a report names no valid bobbin")
    scope = doc.get("scope")
    if scope not in (None, *LABELS):
        raise Refused("a report has an unknown scope")
    if not isinstance(doc.get("ok"), bool):
        raise Refused("a report has no ok flag")
    checks = []
    for c in (doc.get("checks") or [])[:MAX_CHECKS]:
        if not isinstance(c, dict) or not isinstance(c.get("name"), str) or not CHECK_RE.match(c["name"]) \
                or c.get("result") not in RESULTS or not isinstance(c.get("required"), bool):
            raise Refused("a report has a malformed check")
        checks.append({"name": c["name"], "result": c["result"], "required": c["required"],
                       "detail": safe(c.get("detail", "")), "fix": safe(c.get("fix", ""), 200)})
    return {"bobbin": name, "ok": doc["ok"], "scope": scope, "summary": safe(doc.get("summary", "")), "checks": checks}


def decide_label(cards: list[dict]) -> str | None:
    scopes = [c["scope"] for c in cards]
    if not cards or any(s is None for s in scopes):
        return None
    return "touches-core" if "touches-core" in scopes else "bobbin-only"


def comment_body(cards: list[dict], label: str | None) -> str:
    lines = [MARKER, "## Bobbin scorecards", ""]
    if label:
        lines += [f"Label: `{label}`" + (" (every change is inside the bobbin)" if label == "bobbin-only" else
                                        " (files outside the bobbin changed: review those as core changes)"), ""]
    for c in cards:
        lines += [f"### `{c['bobbin']}`: {'PASS' if c['ok'] else 'FAIL'}", "", c["summary"], "",
                  "| Check | Result | Detail |", "|---|---|---|"]
        for k in c["checks"]:
            res = k["result"] if k["result"] != "fail" else ("**FAIL**" if k["required"] else "advisory")
            detail = k["detail"] + (f". How to fix: {k['fix']}" if k["result"] == "fail" and k["fix"] else "")
            lines.append(f"| {k['name']}{'' if k['required'] else ' (advisory)'} | {res} | {detail} |")
        lines.append("")
    lines.append("A green scorecard, `bobbin-only`, and the three boxes in the pull request ticked is the expected"
                 " merge (docs/BOBBIN_REVIEW.md).")
    return "\n".join(lines) + "\n"


def github_api(token: str, root: str = "https://api.github.com") -> Api:
    def call(method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(root.rstrip("/") + path, data=data, method=method, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "hookspool-bobbin-label",
            **({"Content-Type": "application/json"} if data is not None else {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404 and method == "DELETE":
                return None
            raise Refused(f"GitHub API {method} {path.split('?')[0]}: HTTP {e.code}") from None
        return json.loads(raw) if raw.strip() else None
    return call


def find_comment(api: Api, repo: str, pr: int) -> dict | None:
    """The one scorecard comment (by its hidden marker, written by a bot), if there is one."""
    for page in range(1, 11):
        rows = api("GET", f"/repos/{repo}/issues/{pr}/comments?per_page=100&page={page}", None) or []
        for c in rows:
            if isinstance(c, dict) and MARKER in str(c.get("body") or "") and \
                    (c.get("user") or {}).get("type") == "Bot":
                return c
        if len(rows) < 100:
            break
    return None


def clear(api: Api, repo: str, pr: int, head_sha: str, *, missing: list[str] = (), dry_run: bool = False) -> dict:
    """No scorecard arrived: drop the labels an earlier push set and refresh the comment, if any. ``missing``
    names the bobbins meta.json says were checked; with any, the check job died before writing a card (the
    required job is red), and the comment says so instead of "no bobbin"."""
    info = api("GET", f"/repos/{repo}/pulls/{pr}", None)
    if not isinstance(info, dict) or (info.get("head") or {}).get("sha") != head_sha:
        raise Refused("the artifact names a pull request whose head is not this run's commit")
    done: dict[str, Any] = {"pr": pr, "label": None, "comment": None}
    if dry_run:
        done["comment"] = "dry-run"
        return done
    for x in LABELS:
        api("DELETE", f"/repos/{repo}/issues/{pr}/labels/{x}", None)
    existing = find_comment(api, repo, pr)
    if existing is not None:
        text = ("The latest push changes no bobbin." if not missing else
                f"The latest push changes {', '.join(f'`{n}`' for n in missing)} but no scorecard was produced:"
                " the check job stopped before writing one (see its log).")
        api("PATCH", f"/repos/{repo}/issues/comments/{int(existing['id'])}",
            {"body": f"{MARKER}\n## Bobbin scorecards\n\n{text}\n"})
        done["comment"] = "updated"
    return done


def apply(api: Api, repo: str, pr: int, head_sha: str, cards: list[dict], *, dry_run: bool = False) -> dict:
    """Verify the pull request, then set the label and the one comment. Returns what was done."""
    info = api("GET", f"/repos/{repo}/pulls/{pr}", None)
    if not isinstance(info, dict) or (info.get("head") or {}).get("sha") != head_sha:
        raise Refused("the artifact names a pull request whose head is not this run's commit")
    label = decide_label(cards)
    body = comment_body(cards, label)
    done: dict[str, Any] = {"pr": pr, "label": label, "comment": None}
    if dry_run:
        done["comment"] = "dry-run"
        return done
    if label:
        api("POST", f"/repos/{repo}/issues/{pr}/labels", {"labels": [label]})
        other = next(x for x in LABELS if x != label)
        api("DELETE", f"/repos/{repo}/issues/{pr}/labels/{other}", None)
    existing = find_comment(api, repo, pr)
    if existing is not None:
        api("PATCH", f"/repos/{repo}/issues/comments/{int(existing['id'])}", {"body": body})
        done["comment"] = "updated"
    else:
        api("POST", f"/repos/{repo}/issues/{pr}/comments", {"body": body})
        done["comment"] = "created"
    return done


def main(argv: list[str] | None = None, api: Api | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bobbin_label", description=__doc__.split("\n")[0])
    ap.add_argument("--reports", required=True, help="the downloaded artifact directory")
    ap.add_argument("--repo", required=True, help="OWNER/NAME")
    ap.add_argument("--head-sha", required=True, help="the head commit of the run that made the artifact")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        if not REPO_RE.match(a.repo) or not SHA_RE.match(a.head_sha):
            raise Refused("--repo or --head-sha is malformed")
        if not os.path.isdir(a.reports):
            print("bobbin_label: no scorecards (no artifact); nothing to do")
            return 0
        pr, cards, names = read_reports(a.reports)
        if pr is None:
            print("bobbin_label: no pull request in the artifact; nothing to do")
            return 0
        if api is None:
            token = os.environ.get("GH_TOKEN", "")
            if not token:
                raise Refused("GH_TOKEN is not set")
            api = github_api(token, os.environ.get("GITHUB_API_URL") or "https://api.github.com")
        done = (apply(api, a.repo, pr, a.head_sha, cards, dry_run=a.dry_run) if cards
                else clear(api, a.repo, pr, a.head_sha, missing=names, dry_run=a.dry_run))
    except Refused as e:
        print(f"bobbin_label: refused: {e}")
        return 1
    print(f"bobbin_label: pull request #{done['pr']}: label {done['label'] or '-'}, comment {done['comment']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
