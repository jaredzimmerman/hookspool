"""The ``plaid`` bobbin as a unit of a deployment (ARCHITECTURE 14.3): its manifest, that its source calls core and
nothing else (14.3.7: the key fetch and cache are ``hookspool.verify``, the call is ``hookspool/net.py``), how an
instance expands (the ``env`` setting reaches the source), and that its documents (14.3.8) show a configuration that
expands. The behaviour of the source itself is ``tests/test_sources_plaid.py``, which reaches it through
``hookspool.sources.plaid``."""
from __future__ import annotations

import os
import re

import _bobbinrules as R
import pytest

from hookspool import bobbin_loader as PK
from hookspool import config as C

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DIR = os.path.join(ROOT, "hookspool", "bobbins", "plaid", "items")


def package():
    pkgs, errs = PK.discover({})
    assert errs == [], errs
    return pkgs["plaid.items"]


def secret_files(tmp_path):
    out = {}
    for n in ("client_id", "secret"):
        p = tmp_path / f"plaid_{n}"
        p.write_text("test-" + n + "\n")
        os.chmod(p, 0o600)
        out[n] = str(p)
    return out


def load(tmp_path, body):
    p = tmp_path / "config.toml"
    p.write_text(f'[spool]\nhome = "{tmp_path / "home"}"\n[alerts]\ndry = true\n' + body +
                 f'\n[hermes]\nsubscriptions_path = "{tmp_path / "subs.json"}"\n')
    return C.load_config(str(p))


def instance(tmp_path, settings=""):
    s = secret_files(tmp_path)
    return f'''
[bobbins.bank]
bobbin = "plaid.items"
secrets.client_id = {{ file = "{s['client_id']}" }}
secrets.secret = {{ file = "{s['secret']}" }}
{settings}
outputs = {{ "*" = "hermes:bank" }}
'''


def test_the_manifest_is_valid_and_the_source_calls_core_only():
    pkg = package()
    assert pkg.errors == [] and pkg.builtin and pkg.processor is None
    assert set(pkg.secrets) == {"client_id", "secret"} and set(pkg.outputs) == {"*"}
    assert pkg.settings["env"]["default"] == "production" and pkg.source["config"] == ["env"]
    assert R.violations(DIR) == []


def test_the_source_has_no_key_cache_fetch_threads_or_logging_of_its_own():
    text = open(os.path.join(DIR, "source.py"), encoding="utf-8").read()
    for banned in ("import urllib", "import threading", "import logging", "class KeyCache", "import json"):
        assert banned not in text, banned
    assert "v.shared_key_cache(" in text and "v.key_fetcher(" in text


def test_the_env_setting_reaches_the_source_table(tmp_path):
    default = load(tmp_path, instance(tmp_path))["sources"]["bank"]
    assert default["module"] == "hookspool.bobbins.plaid.items.source" and default["action"] == "hermes:bank"
    assert default["env"] == "production" and default["span"] == "sync"
    cfg = load(tmp_path, instance(tmp_path, 'settings = { env = "sandbox" }'))
    assert cfg["sources"]["bank"]["env"] == "sandbox"
    assert [e for e in C.check(cfg).errors if "bank" in e] == []


def test_source_config_must_name_declared_settings(tmp_path):
    spans = {"sync": {}}
    base = {"name": "acme.x", "version": "1", "source": {"module": "source", "config": ["nope"]},
            "outputs": {"*": {"description": "d"}}}
    errs = PK.validate_manifest(base, "acme/x", spans)
    assert any("source.config" in e and "'nope'" in e for e in errs), errs
    base["source"]["config"] = "env"
    assert any("source.config: must be a list" in e for e in PK.validate_manifest(base, "acme/x", spans))
    base["source"] = {"template": {"scheme": "static-token"}, "config": []}
    assert any("only a code source" in e for e in PK.validate_manifest(base, "acme/x", spans))


@pytest.mark.parametrize("reserved", ["module", "action", "secrets", "span", "max_events", "dedup_window_s",
                                      "expect_every_s", "max_body_bytes", "template", "proxy", "config"])
def test_source_config_refuses_the_reserved_source_table_keys(reserved):
    base = {"name": "acme.x", "version": "1", "source": {"module": "source", "config": [reserved]},
            "settings": {reserved: {"type": "string"}}, "outputs": {"*": {"description": "d"}}}
    errs = PK.validate_manifest(base, "acme/x", {"sync": {}})
    assert any("source.config" in e and repr(reserved) in e and "source table key" in e for e in errs), errs
    base["source"]["config"] = ["env"]
    base["settings"] = {"env": {"type": "string", "default": "production"}}
    assert PK.validate_manifest(base, "acme/x", {"sync": {}}) == []


def test_a_bad_env_is_reported_by_check_at_load(tmp_path):
    cfg = load(tmp_path, instance(tmp_path, 'settings = { env = "prod" }'))
    errs = [e for e in C.check(cfg).errors if "bank" in e]
    assert errs and any("env: must be one of production, development, sandbox" in e for e in errs), errs
    good = load(tmp_path, instance(tmp_path, 'settings = { env = "sandbox" }'))
    assert [e for e in C.check(good).errors if "bank" in e] == []


@pytest.mark.parametrize("doc", ["SETUP.md", "AGENTS.md"])
def test_the_configuration_shown_in_the_document_expands(doc, tmp_path):
    text = open(os.path.join(DIR, doc), encoding="utf-8").read()
    (block,) = [b for b in re.findall(r"```toml\n(.*?)```", text, re.S) if "[bobbins.plaid]" in b]
    s = secret_files(tmp_path)
    block = block.replace("/etc/hookspool/secrets/plaid_client_id", s["client_id"])
    block = block.replace("/etc/hookspool/secrets/plaid_secret", s["secret"]).replace("<ROUTE>", "plaid")
    block = block.replace("<ENV>", "sandbox")
    assert not re.search(r"<[A-Z_]+>", block), block
    cfg = load(tmp_path, block)
    assert cfg["sources"]["plaid"]["env"] in ("production", "sandbox")
    for name in ("client_id", "secret", "env"):
        assert name in text
