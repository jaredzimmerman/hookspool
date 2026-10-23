"""The shared source test kit, for the test suite (ARCHITECTURE §14.1, §18).

The kit itself is ``hookspool.testing.sourcekit`` (the bobbin conformance kit runs it too); this
module re-exports it and turns a case that does not apply (``Skip``) into a pytest skip. Test
modules keep using it as before::

    import sourcekit

    @pytest.mark.parametrize("case", sourcekit.CASES, ids=sourcekit.CASE_IDS)
    def test_kit(case, tmp_path, clock):
        sourcekit.run(case, KIT, tmp_path, clock)
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


def run(case, kit: Kit, tmp_path, clock) -> None:
    """Run one matrix case against a fresh environment, then the log checks."""
    try:
        _kit.run(case, kit, tmp_path, clock)
    except Skip as e:
        pytest.skip(str(e))


def captures(source: str) -> list[str]:
    d = os.path.join(CAPTURES, source)
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".http")) if os.path.isdir(d) else []


def run_capture(path: str, kit: Kit, tmp_path, clock) -> dict:
    """``source_test`` on a capture; asserts the ``# expect:`` result. Returns the report."""
    return _kit.run_capture(path, kit, tmp_path, clock)
