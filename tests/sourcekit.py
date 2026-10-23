"""The shared source test kit, for the test suite (ARCHITECTURE §14.1, §18).

The kit itself is ``hookspool.testing.sourcekit`` (the bobbin conformance kit runs it too); this
module re-exports it and turns a case that does not apply (``Skip``) into a pytest skip. Test
modules keep using it as before::

    import sourcekit

    @pytest.mark.parametrize("case", sourcekit.CASES, ids=sourcekit.CASE_IDS)
    def test_kit(case, tmp_path, clock):
        sourcekit.run(case, KIT, tmp_path, clock)

Captures (``tests/captures/<source>/*.http``, or ``hookspool/bobbins/<source>/captures/*.http`` for a built-in bobbin) run through ``run_capture``: the file's
``# expect:`` line is the result ``hookspool source test`` must report.
"""
from __future__ import annotations

import os

import pytest

from hookspool.testing import sourcekit as _kit
from hookspool.testing.sourcekit import (  # noqa: F401 - re-exported for the test modules
    CASE_IDS,
    CASES,
    PLUGIN_FUNCS,
    SENTINEL,
    Env,
    FakeClock,
    Kit,
    Skip,
    request,
)

CAPTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures")
BOBBINS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hookspool", "bobbins")


def run(case, kit: Kit, tmp_path, clock) -> None:
    """Run one matrix case against a fresh environment, then the log checks."""
    try:
        _kit.run(case, kit, tmp_path, clock)
    except Skip as e:
        pytest.skip(str(e))


# ---- captures --------------------------------------------------------------------------------------------------
def capture_dir(source: str) -> str:
    """Where a source's captures live: its bobbin's ``captures/`` when it is a built-in bobbin (ARCHITECTURE 14.3.1),
    else ``tests/captures/<source>``."""
    own = os.path.join(BOBBINS, source, "captures")
    return own if os.path.isdir(own) else os.path.join(CAPTURES, source)


def captures(source: str) -> list[str]:
    d = capture_dir(source)
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".http")) if os.path.isdir(d) else []


def run_capture(path: str, kit: Kit, tmp_path, clock) -> dict:
    """``source_test`` on a capture; asserts the ``# expect:`` result. Returns the report."""
    return _kit.run_capture(path, kit, tmp_path, clock)
