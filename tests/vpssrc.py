"""Locate the production source copies used for parity tests (``vps-src/``, gitignored: it holds
files copied from a private deployment). Tests that need it skip when it is absent, so a public
checkout runs the rest. Looked up in ``$HOOKSPOOL_VPS_SRC``, ``<repo>/vps-src`` and, for a worktree
under ``<repo>/wt/<id>``, ``<repo>/vps-src``."""
from __future__ import annotations

import importlib.util
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def root() -> str | None:
    for c in (os.environ.get("HOOKSPOOL_VPS_SRC"), os.path.join(ROOT, "vps-src"),
              os.path.join(ROOT, "..", "..", "vps-src")):
        if c and os.path.isdir(os.path.join(c, "scripts")):
            return os.path.abspath(c)
    return None


def script(name: str) -> str:
    r = root()
    p = os.path.join(r, "scripts", name) if r else None
    if not p or not os.path.exists(p):
        pytest.skip("vps-src is not present in this checkout")
    return p


def load(name: str, module_name: str | None = None):
    """Import ``vps-src/scripts/<name>`` as a fresh module (skips when absent)."""
    p = script(name)
    spec = importlib.util.spec_from_file_location(module_name or "vps_" + name.removesuffix(".py"), p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def names() -> dict:
    """The deployment names the private originals use (scheduling module, event types, agent state file and address constant),
    read from their source at run time so that this public repo does not spell them. Neutral placeholders
    when vps-src is absent (the parity tests are skipped then)."""
    out = {"sched_module": "orig_sched", "mail_type": "orig-mail", "schedule_type": "orig-schedule",
           "agent_state": "orig_agent_state.json", "agent_attr": "AGENT"}
    r = root()
    p = os.path.join(r, "scripts", "gmail_delta.py") if r else None
    if not p or not os.path.exists(p):
        return out
    with open(p, encoding="utf-8") as f:
        src = f.read()
    for key, pat in (("sched_module", r"^import (\w+_sched)\b"), ("schedule_type", r'"event_type": "([\w-]+-schedule)"'),
                     ("mail_type", r'"([\w-]+-mail)"'), ("agent_state", r'"(gmail_push_state_\w+\.json)"'),
                     ("agent_attr", r'if account == (\w+) and "INBOX"')):
        m = re.search(pat, src, re.M)
        if m:
            out[key] = m.group(1)
    return out
