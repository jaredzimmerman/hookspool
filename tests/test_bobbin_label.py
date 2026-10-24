"""``.github/scripts/bobbin_label.py``: the labelling job reads untrusted scorecards safely.

Against a fake GitHub API: the pull request is verified against the run's head commit before anything is
written, the label comes from GitHub's list of the pull request's files (the artifact's scope is only a
cross-check), only the two labels are ever set, one comment is created or updated, and text from the artifact
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


ACME_PR = ["hookspool/bobbins/acme/bobbin.toml", "hookspool/bobbins/acme/captures/event.http",
           "hookspool/bobbins/acme/README.md", "tests/bobbins/test_acme.py"]


class FakeApi:
    """GitHub, as far as the label job sees it. ``files`` is the pull request's file list (dicts as GitHub sends
    them, or bare names), served 100 per page."""

    def __init__(self, head=SHA, comments=(), files=ACME_PR):
        self.head = head
        self.comments = list(comments)
        self.files = [f if isinstance(f, dict) else {"filename": f, "status": "modified"} for f in files]
        self.calls: list[tuple[str, str, object]] = []

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if method == "GET" and path.endswith("/pulls/7"):
            return {"head": {"sha": self.head}}
        if method == "GET" and "/pulls/7/files?" in path:
            assert "per_page=100" in path
            page = int(path.rsplit("page=", 1)[1])
            return self.files[(page - 1) * 100:page * 100]
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
    api = FakeApi(comments=[forged, mine], files=ACME_PR + ["hookspool/bobbins/other/bobbin.toml"])
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


def test_a_bobbin_without_a_scorecard_is_named_instead_of_no_bobbin(tmp_path):
    mine = {"id": 99, "body": L.MARKER + "\nold", "user": {"type": "Bot"}}
    api = FakeApi(comments=[mine])
    d = tmp_path / "reports"
    d.mkdir()
    (d / "meta.json").write_text(json.dumps({"pr": 7, "bobbins": ["acme", "bad name", 3]}))   # the child died: no card
    assert L.main(["--reports", str(d), "--repo", REPO, "--head-sha", SHA], api=api) == 0
    body = next(b["body"] for m, p, b in api.calls if m == "PATCH" and p.endswith("/comments/99"))
    assert "`acme`" in body and "no scorecard was produced" in body and "changes no bobbin" not in body
    assert "bad name" not in body
    deleted = {p.rsplit("/", 1)[1] for m, p in api.writes() if m == "DELETE"}
    assert deleted == {"bobbin-only", "touches-core"}
    assert not any(m == "POST" for m, _p in api.writes())


def test_a_dead_check_next_to_a_surviving_card_is_named_and_sets_no_label(tmp_path):
    api = FakeApi()
    d = artifact(tmp_path, [card("acme")])
    (tmp_path / "reports" / "meta.json").write_text(json.dumps({"pr": 7, "bobbins": ["acme", "gone"]}))
    assert L.main(["--reports", d, "--repo", REPO, "--head-sha", SHA], api=api) == 0
    body = next(b for m, p, b in api.calls if p.endswith("/comments") and m == "POST")["body"]
    assert "### `acme`: PASS" in body and "No scorecard for `gone`" in body and "No label until" in body
    assert "Label:" not in body
    assert not any(p.endswith("/labels") and m == "POST" for m, p in api.writes())
    deleted = {p.rsplit("/", 1)[1] for m, p in api.writes() if m == "DELETE"}
    assert deleted == {"bobbin-only", "touches-core"}


def label_of(api) -> str:
    (labels,) = [b["labels"] for m, p, b in api.calls if m == "POST" and p.endswith("/labels")]
    return labels[0]


def comment_of(api) -> str:
    return next(b for m, p, b in api.calls if p.endswith("/comments") and m == "POST")["body"]


def test_a_one_bobbin_pull_request_is_labelled_bobbin_only(tmp_path):
    """A new bobbin made the way docs/bobbins/writing-a-bobbin.md says: its directory and its test file, nothing in
    README.md or CHANGELOG.md."""
    api = FakeApi(files=ACME_PR)
    assert L.main(["--reports", artifact(tmp_path, [card()]), "--repo", REPO, "--head-sha", SHA], api=api) == 0
    assert label_of(api) == "bobbin-only"
    assert "GitHub's list" not in comment_of(api)


def test_the_label_comes_from_github_not_from_the_scorecard(tmp_path):
    """The scorecards are the pull request's own output: one that edits the kit can claim bobbin-only. GitHub's
    list of the files decides, and the comment says that the two disagree."""
    api = FakeApi(files=ACME_PR + [".github/scripts/plugin_ci.py"])
    assert L.main(["--reports", artifact(tmp_path, [card()]), "--repo", REPO, "--head-sha", SHA], api=api) == 0
    assert label_of(api) == "touches-core"
    body = comment_of(api)
    assert "The scorecards say `bobbin-only` but GitHub's list of the changed files says `touches-core`" in body


def test_a_scorecard_that_claims_touches_core_for_a_bobbin_only_diff_is_still_touches_core(tmp_path):
    api = FakeApi(files=ACME_PR)
    assert L.main(["--reports", artifact(tmp_path, [card(scope="touches-core")]), "--repo", REPO, "--head-sha", SHA],
                  api=api) == 0
    assert label_of(api) == "touches-core" and "GitHub's list of the changed files says `bobbin-only`" in comment_of(api)


def test_a_scorecard_without_a_scope_cannot_confirm_bobbin_only(tmp_path):
    api = FakeApi()
    assert L.main(["--reports", artifact(tmp_path, [card(scope=None)]), "--repo", REPO, "--head-sha", SHA], api=api) == 0
    assert label_of(api) == "touches-core" and "The scorecards say `no scope`" in comment_of(api)


def test_every_page_of_the_file_list_is_read(tmp_path):
    files = [f"hookspool/bobbins/acme/captures/c{i}.http" for i in range(100)] + ["hookspool/store.py"]
    api = FakeApi(files=files)
    assert L.main(["--reports", artifact(tmp_path, [card()]), "--repo", REPO, "--head-sha", SHA], api=api) == 0
    pages = [p for m, p, _ in api.calls if "/files?" in p]
    assert [p.rsplit("page=", 1)[1] for p in pages] == ["1", "2"]
    assert label_of(api) == "touches-core"


def test_a_file_list_at_githubs_limit_is_touches_core(tmp_path):
    api = FakeApi(files=[f"hookspool/bobbins/acme/captures/c{i}.http" for i in range(3000)])
    assert L.main(["--reports", artifact(tmp_path, [card()]), "--repo", REPO, "--head-sha", SHA], api=api) == 0
    assert len([p for m, p, _ in api.calls if "/files?" in p]) == L.MAX_FILE_PAGES
    assert label_of(api) == "touches-core" and "only the first 3000 files" in comment_of(api)


def test_a_rename_out_of_core_counts_its_old_name(tmp_path):
    moved = {"filename": "hookspool/bobbins/acme/helper.py", "previous_filename": "hookspool/net.py",
             "status": "renamed"}
    api = FakeApi(files=ACME_PR + [moved])
    assert L.main(["--reports", artifact(tmp_path, [card()]), "--repo", REPO, "--head-sha", SHA], api=api) == 0
    assert label_of(api) == "touches-core"


def test_a_malformed_file_list_is_refused(tmp_path):
    api = FakeApi(files=[{"status": "added"}])
    assert L.main(["--reports", artifact(tmp_path, [card()]), "--repo", REPO, "--head-sha", SHA], api=api) == 1
    assert api.writes() == []


@pytest.mark.parametrize("files,scope", [
    (ACME_PR, "bobbin-only"),
    (["tests/bobbins/test_acme.py"], "bobbin-only"),
    (["hookspool/bobbins/web-hook/bobbin.toml", "tests/bobbins/test_web_hook.py"], "bobbin-only"),
    (["hookspool/bobbins/acme/bobbin.toml", "tests/bobbins/test_other.py"], "touches-core"),
    (["hookspool/bobbins/acme/bobbin.toml", "hookspool/bobbins/other/bobbin.toml"], "touches-core"),
    (["hookspool/bobbins/__init__.py"], "touches-core"),
    (["hookspool/bobbins/acme/bobbin.toml", "README.md"], "touches-core"),
    (["hookspool/bobbins/acme/bobbin.toml", "CHANGELOG.md"], "touches-core"),
    (["contrib/acme/bobbin.toml"], "touches-core"),
    ([], "touches-core"),
    (None, "touches-core"),
])
def test_scope_of_files(files, scope):
    assert L.scope_of_files(files) == scope


@pytest.mark.parametrize("files", [
    ACME_PR, ["tests/bobbins/test_acme.py"], ["hookspool/bobbins/acme/x.py", "README.md"],
    ["hookspool/bobbins/acme/x.py", "CHANGELOG.md"], ["hookspool/bobbins/acme/x.py", "hookspool/bobbins/other/y.py"],
    ["hookspool/bobbins/acme/x.py", "tests/bobbins/test_other.py"], ["docs/bobbins/README.md"],
])
def test_the_label_rule_is_the_kits_scope_rule(files):
    """One rule in two places: the label job (trusted, GitHub's list) and the kit's advisory ``scope`` check (the
    checkout's list) agree on every built-in bobbin's pull request."""
    from hookspool.testing import bobbinkit
    outside = bobbinkit.outside_bobbin(files, "hookspool/bobbins/acme/", "tests/bobbins/test_acme.py")
    assert (L.scope_of_files(files) == "bobbin-only") == (not outside), outside


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
