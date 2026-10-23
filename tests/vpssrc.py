"""Locate the production source copies used for parity tests (``vps-src/``, gitignored: it holds
files copied from a private deployment). Tests that need it skip when it is absent, so a public
checkout runs the rest. Looked up in ``$HOOKSPOOL_VPS_SRC``, ``<repo>/vps-src`` and, for a worktree
under ``<repo>/wt/<id>``, ``<repo>/vps-src``."""
from __future__ import annotations

import importlib.util
import os

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

