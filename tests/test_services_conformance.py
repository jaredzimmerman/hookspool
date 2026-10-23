"""Every built-in bobbin passes the conformance kit (ARCHITECTURE 14.3.5).

The same scorecard ``hookspool bobbin check hookspool/bobbins/<name>`` prints, and the one CI runs on
every changed bobbin; a required check that fails here fails the suite.
"""
from __future__ import annotations

import glob
import os

import pytest

from hookspool import bobbin_loader as BL
from hookspool.testing import bobbinkit as K

BUILTIN = sorted(os.path.dirname(p) for p in glob.glob(os.path.join(BL.BUILTIN_DIR, "*", BL.MANIFEST)))
PARAMS = [pytest.param(d, id=os.path.basename(d)) for d in BUILTIN] or \
    [pytest.param(None, id="none", marks=pytest.mark.skip(reason="no built-in bobbins yet"))]


@pytest.mark.parametrize("bobbin_dir", PARAMS)
def test_builtin_bobbin_passes_the_conformance_kit(bobbin_dir):
    card = K.check_bobbin(bobbin_dir)
    assert card.ok, K.render_text(card.as_dict())
    assert card.bobbin == os.path.basename(bobbin_dir)
