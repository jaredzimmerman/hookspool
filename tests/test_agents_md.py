"""AGENTS.md against the repository: every command and path it names exists, and CLAUDE.md imports it."""
from __future__ import annotations

import argparse
import glob
import os
import re

from hookspool import __main__ as cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name: str) -> str:
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def _spans(text: str) -> list[str]:
    """Every inline code span and every line of a fenced block."""
    out = []
    for m in re.finditer(r"```[^\n]*\n(.*?)```|`([^`\n]+)`", text, re.S):
        out += (m.group(1) or m.group(2)).splitlines()
    return out


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    sub = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
    return dict(sub.choices) if sub else {}


def test_every_hookspool_command_named_in_agents_md_exists():
    top = _subcommands(cli._parser())
    seen = set()
    for ln in _spans(read("AGENTS.md")):
        for m in re.finditer(r"(?<![\w/.-])hookspool((?:\s+--(?:home|config)\s+\S+|\s+--json)*)\s+([a-z]+)\b(?:\s+([a-z]+)\b)?", ln):
            cmd, nxt = m.group(2), m.group(3)
            assert cmd in top, f"`hookspool {cmd}` is not a command: {ln.strip()!r}"
            seen.add(cmd)
            nested = _subcommands(top[cmd])
            if nested and nxt:
                assert nxt in nested, f"`hookspool {cmd} {nxt}` is not a subcommand"
    # the commands the guide's procedures depend on
    assert {"check", "reload", "status", "list", "issues", "bobbin", "migrate", "drop", "pause"} <= seen


def test_every_path_named_in_agents_md_exists():
    text = read("AGENTS.md")
    paths = {s for s in re.findall(r"`([^`\s]+)`", text)}
    paths |= set(re.findall(r"\]\(([^)\s#]+)", text))
    checked = 0
    for p in sorted(paths):
        if p == "deploy/local/":        # git-ignored by design: a deployment's own files
            continue
        if "://" in p or p.startswith(("/", "$", "-", "[", "@")) or "=" in p:
            continue
        if not (("/" in p) or re.search(r"\.(md|toml|py|txt|in|yml)$", p)):
            continue
        pattern = re.sub(r"<[^>]*>", "*", p.rstrip("/"))
        if "..." in pattern or pattern.startswith("dispatch.lock"):
            continue
        # a bare file name (SETUP.md) is a file every bobbin has: it must exist somewhere in the tree
        hits = glob.glob(os.path.join(ROOT, pattern if "/" in pattern else f"**/{pattern}"), recursive=True)
        # a placeholder path for a file that does not exist yet (a new bobbin's test) matches its directory
        if not hits and "*" in pattern:
            hits = glob.glob(os.path.dirname(os.path.join(ROOT, pattern)))
        assert hits, f"AGENTS.md names {p}, which does not exist"
        checked += 1
    assert checked >= 20


def test_claude_md_imports_agents_md():
    lines = read("CLAUDE.md").splitlines()
    assert lines[0].strip() == "@AGENTS.md"
    assert len(read("CLAUDE.md").strip().splitlines()) <= 4


def test_agents_md_is_short_and_linked_from_the_front_pages():
    assert len(read("AGENTS.md").splitlines()) <= 200
    assert "AGENTS.md" in read("README.md") and "AGENTS.md" in read("CONTRIBUTING.md")


def test_agents_md_has_both_parts():
    text = read("AGENTS.md")
    assert "## Part 1: Working on this repository" in text and "## Part 2: Operating hookspool for a user" in text
