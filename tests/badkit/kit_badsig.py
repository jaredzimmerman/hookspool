"""Kit test for ``tests/badkit/badsig.py``. Not collected by a plain ``pytest`` run (no ``test_`` prefix);
``tests/test_ci_tools.py`` runs it through ``.github/scripts/plugin_ci.py`` and expects it to FAIL."""
import hashlib
import hmac
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sourcekit  # noqa: E402
from badkit import badsig  # noqa: E402
from sourcekit import SENTINEL, Kit, request  # noqa: E402

SECRET = "test-kit-secret-bad-" + SENTINEL


def sign(raw, secrets, now):
    mac = hmac.new(secrets["secret"].encode(), raw.body, hashlib.sha256).hexdigest()
    return raw.set("X-Kit-Signature", "sha256=" + mac)


KIT = Kit(name="badsig", module=badsig, secrets={"secret": SECRET}, sign=sign,
          sample=request("POST", "/hooks/badsig", {"Content-Type": "application/json", "X-Kit-Event": "push"},
                         b'{"repo":"octo/widgets","note":"' + SENTINEL.encode() + b'"}'))


@pytest.mark.parametrize("case", sourcekit.CASES, ids=sourcekit.CASE_IDS)
def test_kit(case, tmp_path, clock):
    sourcekit.run(case, KIT, tmp_path, clock)
