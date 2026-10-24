"""Every built-in bobbin passes the conformance kit (ARCHITECTURE 14.3.5).

The same scorecard ``hookspool bobbin check hookspool/bobbins/<vendor>/<product>`` prints, and the one CI runs on
every changed bobbin; a required check that fails here fails the suite. The name and provider rows are asserted on
their own: every built-in is ``<vendor>.<product>`` in ``<vendor>/<product>/`` and links its provider's pages.
"""
from __future__ import annotations

import glob
import os

import pytest

from hookspool import bobbin_loader as BL
from hookspool.testing import bobbinkit as K

BUILTIN = sorted(os.path.dirname(p) for p in glob.glob(os.path.join(BL.BUILTIN_DIR, "*", "*", BL.MANIFEST)))
NAMES = {"google.gmail", "google.calendar", "google.drive", "github.repo", "atlassian.trello", "kaneo.projects",
         "zoom.meetings", "plaid.items", "hookspool.webhook", "google.meet", "cloudflare.notifications",
         "finnhub.earnings", "courtlistener.alerts"}


def _name(d: str) -> str:
    return f"{os.path.basename(os.path.dirname(d))}.{os.path.basename(d)}"


def test_the_built_ins_are_the_namespaced_bobbins():
    assert {_name(d) for d in BUILTIN} == NAMES
    flat = [p for p in glob.glob(os.path.join(BL.BUILTIN_DIR, "*", BL.MANIFEST))]
    assert flat == [], "a built-in sits in hookspool/bobbins/<vendor>/<product>/"
    for vendor in {os.path.dirname(d) for d in BUILTIN}:
        assert sorted(f for f in os.listdir(vendor) if os.path.isfile(os.path.join(vendor, f))) == ["__init__.py"]
        assert open(os.path.join(vendor, "__init__.py")).read() == "", vendor


@pytest.mark.parametrize("bobbin_dir", BUILTIN, ids=[_name(d) for d in BUILTIN])
def test_builtin_bobbin_passes_the_conformance_kit(bobbin_dir):
    card = K.check_bobbin(bobbin_dir)
    assert card.ok, K.render_text(card.as_dict())
    assert card.bobbin == _name(bobbin_dir)
    by = {c.name: c for c in card.checks}
    assert by["name"].result == "pass" and by["provider"].result == "pass", (by["name"].detail, by["provider"].detail)
    assert by["provider-links"].result == "skip"                     # offline unless --online
