"""The ``finnhub.earnings`` bobbin as a unit of a deployment (ARCHITECTURE 14.3): its manifest (a fixed shared-secret
template with no Python), that an instance expands to a template source with the bobbin's span and the mapped action,
and that the configuration its documents show expands. The captures are checked by ``hookspool bobbin check``."""
from __future__ import annotations

import os
import re

import _bobbinrules as R
import pytest

from hookspool import bobbin_loader as PK
from hookspool import config as C

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DIR = os.path.join(ROOT, "hookspool", "bobbins", "finnhub", "earnings")


def package():
    pkgs, errs = PK.discover({})
    assert errs == [], errs
    return pkgs["finnhub.earnings"]


def load(tmp_path, body):
    p = tmp_path / "config.toml"
    p.write_text(f'[spool]\nhome = "{tmp_path / "home"}"\n[alerts]\ndry = true\n' + body +
                 f'\n[hermes]\nsubscriptions_path = "{tmp_path / "subs.json"}"\n')
    return C.load_config(str(p))


def secret(tmp_path):
    p = tmp_path / "earnings.secret"
    p.write_text("test-finnhub-secret\n")
    os.chmod(p, 0o600)
    return str(p)


def test_the_manifest_is_a_fixed_shared_secret_template_with_no_python():
    pkg = package()
    assert pkg.errors == [] and pkg.builtin and pkg.processor is None
    assert set(pkg.secrets) == {"secret"} and pkg.settings == {} and set(pkg.outputs) == {"*"}
    assert (pkg.source["span"], pkg.source.get("sender_retries", False)) == ("immediate", False)
    assert pkg.source["template"]["scheme"] == "shared-secret-header"
    assert pkg.source["template"]["header"] == "X-Finnhub-Secret"
    assert R.violations(DIR) == []
    assert not os.path.exists(os.path.join(DIR, "source.py")) and not os.path.exists(os.path.join(DIR, "processor.py"))


def test_an_instance_expands_to_a_template_source_with_the_mapped_action(tmp_path):
    cfg = load(tmp_path, f'''
[bobbins.earnings]
bobbin = "finnhub.earnings"
secrets.secret = {{ file = "{secret(tmp_path)}" }}
outputs = {{ "*" = "hermes:earnings" }}
expect = false
''')
    src = cfg["sources"]["earnings"]
    assert src["scheme"] == "shared-secret-header" and src["header"] == "X-Finnhub-Secret"
    assert src["key"] == "data.0.symbol" and src["dedup"] == "body_sha256"
    assert src["span"] == "immediate" and src["action"] == "hermes:earnings"
    assert "script:earnings" not in cfg["destinations"]


@pytest.mark.parametrize("doc", ["SETUP.md", "AGENTS.md"])
def test_the_configuration_shown_in_the_document_expands(doc, tmp_path):
    text = open(os.path.join(DIR, doc), encoding="utf-8").read()
    (block,) = [b for b in re.findall(r"```toml\n(.*?)```", text, re.S) if "[bobbins.earnings]" in b]
    block = block.replace("/etc/hookspool/secrets/earnings_secret", secret(tmp_path)).replace("<ROUTE>", "earnings")
    assert not re.search(r"<[A-Z_]+>", block), block
    cfg = load(tmp_path, block)
    assert "earnings" in cfg["sources"] and "earnings_secret" in text
