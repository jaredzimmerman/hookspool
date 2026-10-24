"""The ``zoom`` bobbin as a unit of a deployment (ARCHITECTURE 14.3): its manifest, that its source calls core and
nothing else (14.3.7), how an instance expands, and that its documents (14.3.8) show a configuration that expands.
The behaviour of the source itself is ``tests/test_sources_zoom.py``, which reaches it through
``hookspool.sources.zoom``."""
from __future__ import annotations

import os
import re

import _bobbinrules as R
import pytest

from hookspool import bobbin_loader as PK
from hookspool import config as C

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DIR = os.path.join(ROOT, "hookspool", "bobbins", "zoom", "meetings")


def package():
    pkgs, errs = PK.discover({})
    assert errs == [], errs
    return pkgs["zoom.meetings"]


def load(tmp_path, body):
    p = tmp_path / "config.toml"
    p.write_text(f'[spool]\nhome = "{tmp_path / "home"}"\n[alerts]\ndry = true\n' + body +
                 f'\n[hermes]\nsubscriptions_path = "{tmp_path / "subs.json"}"\n')
    return C.load_config(str(p))


def secret(tmp_path):
    p = tmp_path / "zoom.secret"
    p.write_text("test-zoom-secret\n")
    os.chmod(p, 0o600)
    return str(p)


def test_the_manifest_is_valid_and_the_source_calls_core_only():
    pkg = package()
    assert pkg.errors == [] and pkg.builtin and pkg.processor is None
    assert set(pkg.secrets) == {"webhook_secret"} and pkg.settings == {} and set(pkg.outputs) == {"*"}
    assert (pkg.source["span"], pkg.source["sender_retries"]) == ("immediate", True)
    assert R.violations(DIR) == []


def test_the_source_answers_its_hmac_and_its_validation_through_core():
    """No ``hmac`` of its own: the signature check and the validation answer are ``hookspool.verify``."""
    text = open(os.path.join(DIR, "source.py"), encoding="utf-8").read()
    assert "v.timestamped(" in text and "v.hmac_hex_sign(" in text and "import hmac" not in text


def test_an_instance_expands_to_a_source_with_the_mapped_action(tmp_path):
    cfg = load(tmp_path, f'''
[bobbins.meetings]
bobbin = "zoom.meetings"
secrets.webhook_secret = {{ file = "{secret(tmp_path)}" }}
outputs = {{ "*" = "hermes:meetings" }}
expect = false
''')
    src = cfg["sources"]["meetings"]
    assert src["module"] == "hookspool.bobbins.zoom.meetings.source" and src["action"] == "hermes:meetings"
    assert src["span"] == "immediate" and "script:meetings" not in cfg["destinations"]


@pytest.mark.parametrize("doc", ["SETUP.md", "AGENTS.md"])
def test_the_configuration_shown_in_the_document_expands(doc, tmp_path):
    text = open(os.path.join(DIR, doc), encoding="utf-8").read()
    (block,) = [b for b in re.findall(r"```toml\n(.*?)```", text, re.S) if "[bobbins.meetings]" in b]
    block = block.replace("/etc/hookspool/secrets/zoom_webhook_secret", secret(tmp_path)).replace("<ROUTE>", "zoom")
    assert not re.search(r"<[A-Z_]+>", block), block
    cfg = load(tmp_path, block)
    assert "meetings" in cfg["sources"] and "webhook_secret" in text
