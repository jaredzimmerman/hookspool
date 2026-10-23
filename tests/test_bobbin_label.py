"""``.github/scripts/bobbin_label.py``: the labelling job reads untrusted scorecards safely.

Against a fake GitHub API: the pull request is verified against the run's head commit before anything is
written, only the two labels are ever set, one comment is created or updated, and text from the artifact
cannot mention people, add links or break the table.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("bobbin_label", os.path.join(ROOT, ".github", "scripts", "bobbin_label.py"))
L = importlib.util.module_from_spec(spec)
sys.modules["bobbin_label"] = L
spec.loader.exec_module(L)

SHA = "a" * 40
REPO = "owner/hookspool"


def card(name="acme", ok=True, scope="bobbin-only", detail="fine", **over):
    doc = {"schema": L.SCHEMA, "bobbin": name, "dir": f"hookspool/bobbins/{name}", "ok": ok, "strict": False,
           "scope": scope, "summary": f"bobbin check {name}: {'PASS' if ok else 'FAIL'}",
           "checks": [{"name": "manifest", "required": True, "result": "pass", "detail": detail, "fix": "",
                       "findings": []},
                      {"name": "scope", "required": False, "result": "pass" if scope == "bobbin-only" else "fail",
                       "detail": "x", "fix": "split it", "findings": []}]}
    doc.update(over)
    return doc


def artifact(tmp_path, cards, pr=7):
    d = tmp_path / "reports"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"pr": pr, "bobbins": [c["bobbin"] for c in cards]}))
    for c in cards:
        (d / f"{c['bobbin']}.json").write_text(json.dumps(c))
    return str(d)


class FakeApi:
    def __init__(self, head=SHA, comments=()):
        self.head = head
        self.comments = list(comments)
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if method == "GET" and path.endswith("/pulls/7"):
            return {"head": {"sha": self.head}}
        if method == "GET" and "/comments" in path:
            return self.comments
        return {}

    def writes(self):
        return [(m, p) for m, p, _ in self.calls if m != "GET"]


def test_labels_and_creates_one_comment(tmp_path):
    api = FakeApi()
    assert L.main(["--reports", artifact(tmp_path, [card()]), "--repo", REPO, "--head-sha", SHA], api=api) == 0
    assert api.writes() == [("POST", f"/repos/{REPO}/issues/7/labels"), ("DELETE", f"/repos/{REPO}/issues/7/labels/touches-core"),
                            ("POST", f"/repos/{REPO}/issues/7/comments")]
    label_body = next(b for m, p, b in api.calls if p.endswith("/labels"))
    assert label_body == {"labels": ["bobbin-only"]}
    body = next(b for m, p, b in api.calls if p.endswith("/comments") and m == "POST")["body"]
    assert body.startswith(L.MARKER) and "### `acme`: PASS" in body and "| manifest | pass | fine |" in body


def test_touches_core_wins_and_an_existing_comment_is_updated(tmp_path):
    mine = {"id": 99, "body": L.MARKER + "\nold", "user": {"type": "Bot"}}
    forged = {"id": 5, "body": L.MARKER, "user": {"type": "User"}}         # a person pasting the marker
    api = FakeApi(comments=[forged, mine])
    cards = [card("acme"), card("other", scope="touches-core")]
    assert L.main(["--reports", artifact(tmp_path, cards), "--repo", REPO, "--head-sha", SHA], api=api) == 0
    assert ("PATCH", f"/repos/{REPO}/issues/comments/99") in api.writes()
    assert not any(p.endswith("/issues/7/comments") and m == "POST" for m, p in api.writes())
    assert next(b for m, p, b in api.calls if p.endswith("/labels")) == {"labels": ["touches-core"]}
    assert ("DELETE", f"/repos/{REPO}/issues/7/labels/bobbin-only") in api.writes()


def test_refuses_when_the_pull_request_head_is_not_the_runs(tmp_path, capsys):
    api = FakeApi(head="b" * 40)
    assert L.main(["--reports", artifact(tmp_path, [card()]), "--repo", REPO, "--head-sha", SHA], api=api) == 1
    assert api.writes() == [] and "refused" in capsys.readouterr().out


@pytest.mark.parametrize("bad", [
    {"schema": "other"}, {"bobbin": "Bad Name"}, {"bobbin": "a" * 41}, {"scope": "admin"}, {"ok": "yes"},
    {"checks": [{"name": "x; rm", "result": "pass", "required": True}]},
    {"checks": [{"name": "manifest", "result": "maybe", "required": True}]},
])
def test_refuses_a_malformed_scorecard(tmp_path, bad):
    api = FakeApi()
    assert L.main(["--reports", artifact(tmp_path, [card(**bad)]), "--repo", REPO, "--head-sha", SHA], api=api) == 1
    assert api.calls == []


def test_refuses_a_bad_pr_number_and_bad_arguments(tmp_path):
    api = FakeApi()
    assert L.main(["--reports", artifact(tmp_path, [card()], pr="7; x"), "--repo", REPO, "--head-sha", SHA], api=api) == 1
    assert L.main(["--reports", str(tmp_path), "--repo", "not a repo", "--head-sha", SHA], api=api) == 1
    assert L.main(["--reports", str(tmp_path), "--repo", REPO, "--head-sha", "HEAD"], api=api) == 1
    assert api.calls == []


def test_nothing_to_do_without_an_artifact_or_a_bobbin(tmp_path):
    api = FakeApi()
    assert L.main(["--reports", str(tmp_path / "absent"), "--repo", REPO, "--head-sha", SHA], api=api) == 0
    assert api.calls == []


def test_a_push_that_touches_no_bobbin_removes_the_old_labels(tmp_path):
    api = FakeApi()
    assert L.main(["--reports", artifact(tmp_path, []), "--repo", REPO, "--head-sha", SHA], api=api) == 0
    deleted = {p.rsplit("/", 1)[1] for m, p in api.writes() if m == "DELETE"}
    assert deleted == {"bobbin-only", "touches-core"}
    assert not any(m == "POST" for m, _p in api.writes())      # no new label, no new comment


def test_no_label_when_a_scorecard_has_no_scope(tmp_path):
    api = FakeApi()
    assert L.main(["--reports", artifact(tmp_path, [card(scope=None)]), "--repo", REPO, "--head-sha", SHA], api=api) == 0
    assert not any(p.endswith("/labels") or "/labels/" in p for _m, p in api.writes())
    assert any(p.endswith("/comments") for _m, p in api.writes())


def test_untrusted_text_cannot_mention_link_or_break_the_table(tmp_path):
    nasty = "@maintainers see [here](https://evil.example.com) <img src=x> | col `code`"
    api = FakeApi()
    L.main(["--reports", artifact(tmp_path, [card(detail=nasty, summary=nasty)]), "--repo", REPO, "--head-sha", SHA],
           api=api)
    body = next(b for m, p, b in api.calls if p.endswith("/comments") and m == "POST")["body"]
    assert "@maintainers" not in body and "](" not in body and "<img" not in body and "https://evil" not in body
    row = next(ln for ln in body.splitlines() if ln.startswith("| manifest |"))
    assert row.count(" | ") == 2 and row.endswith(" |"), row
    assert "`code`" not in body


def test_dry_run_writes_nothing(tmp_path):
    api = FakeApi()
    assert L.main(["--reports", artifact(tmp_path, [card()]), "--repo", REPO, "--head-sha", SHA, "--dry-run"],
                  api=api) == 0
    assert api.writes() == []


def test_the_scorecard_schema_is_the_kits():
    from hookspool.testing import bobbinkit
    assert L.SCHEMA == bobbinkit.SCHEMA
    assert set(L.LABELS) == {"bobbin-only", "touches-core"}
