"""The ``cloudflare.notifications`` template bobbin (ARCHITECTURE 14.3): its manifest loads with no errors and
names its provider, its captures produce the results they declare, a wrong secret is refused, a sample instance
expands through the real config path, and the setup documents show a configuration that expands and name every
secret. No payload or secret value reaches the intake's output."""
from __future__ import annotations

import json
import os
import re
import tomllib

import pytest

from hookspool import bobbin_loader as PK
from hookspool import config as C
from hookspool import intake as I
from hookspool.plugin import parse_capture
from hookspool.testing.sourcekit import SENTINEL

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DIR = os.path.join(ROOT, "hookspool", "bobbins", "cloudflare", "notifications")
TEST_SECRET = "test-template-secret-PAYLOAD-SENTINEL-7f3a"          # the value in captures/kit.toml; no sentinel in a header
INSTANCE = "cloudflare-alerts"
FENCE = re.compile(r"```toml\n(.*?)```", re.S)


def manifest() -> dict:
    with open(os.path.join(DIR, "bobbin.toml"), "rb") as f:
        return tomllib.load(f)


def doc(name: str) -> str:
    with open(os.path.join(DIR, name), encoding="utf-8") as f:
        return f.read()


def write_secret(path, value: str = TEST_SECRET) -> str:
    path.write_text(value)
    os.chmod(path, 0o600)
    return str(path)


def expanded(instances: dict) -> dict:
    return PK.expand(C.merge(C.DEFAULTS, {"bobbins": instances, "alerts": {"argv": ["true"]}}))


def spec(secret: str) -> dict:
    return {"bobbin": "cloudflare.notifications", "secrets": {"secret": {"file": secret}},
            "outputs": {"*": f"hermes:{INSTANCE}"}}


def captures() -> list[str]:
    d = os.path.join(DIR, "captures")
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.endswith(".http"))


def test_the_manifest_is_a_template_with_the_cloudflare_rules():
    pkgs, errs = PK.discover(C.merge(C.DEFAULTS, {}))
    assert errs == [], errs
    pkg = pkgs["cloudflare.notifications"]
    assert pkg.errors == [] and pkg.builtin and pkg.processor is None
    assert "template" in pkg.source and "module" not in pkg.source
    assert set(pkg.secrets) == {"secret"} and set(pkg.outputs) == {"*"}
    m = manifest()
    assert m["provider"]["vendor"] == "Cloudflare"
    assert m["provider"]["webhook_docs"].startswith("https://developers.cloudflare.com/")
    assert m["source"]["sender_retries"] is False and m["source"]["span"] == "burst"
    tmpl = m["source"]["template"]
    assert (tmpl["scheme"], tmpl["header"], tmpl["key"], tmpl["dedup"]) == (
        "shared-secret-header", "cf-webhook-auth", "name", "body_sha256")


def test_the_captures_declare_the_outcomes_the_bobbin_promises():
    expects = {os.path.basename(c): parse_capture(open(c, "rb").read()).meta["expect"] for c in captures()}
    assert expects == {"event.http": "queued", "wrong_secret.http": "unverified"}


@pytest.mark.parametrize("cap", captures(), ids=lambda c: os.path.basename(c))
def test_each_capture_runs_through_the_intake(cap, tmp_path):
    cfg = expanded({INSTANCE: spec(write_secret(tmp_path / "secret"))})
    raw = parse_capture(open(cap, "rb").read())
    out = I.source_test(cfg, INSTANCE, raw)
    assert out["result"] == raw.meta["expect"], out
    assert SENTINEL not in json.dumps(out, default=str)


def test_a_wrong_secret_is_refused_with_401(tmp_path):
    cfg = expanded({INSTANCE: spec(write_secret(tmp_path / "wrong", TEST_SECRET + "-wrong"))})
    raw = parse_capture(open(os.path.join(DIR, "captures", "event.http"), "rb").read())
    out = I.source_test(cfg, INSTANCE, raw)
    assert out["result"] == "unverified" and out["status"] == 401, out


def test_a_missing_secret_is_an_expansion_error(tmp_path):
    s = spec(write_secret(tmp_path / "secret"))
    del s["secrets"]
    with pytest.raises(PK.BobbinError) as ei:
        expanded({INSTANCE: s})
    assert any(f"bobbins.{INSTANCE}.secrets.secret: required by bobbin" in e for e in ei.value.errors)


def test_an_instance_expands_to_a_template_source_with_the_mapped_action(tmp_path):
    secret = write_secret(tmp_path / "secret")
    src = expanded({INSTANCE: spec(secret)})["sources"][INSTANCE]
    assert src["module"] == "hookspool.sources.template"
    assert src["action"] == f"hermes:{INSTANCE}"
    assert src["secrets"] == {"secret": {"file": secret}}


@pytest.mark.parametrize("name", ["README.md", "SETUP.md", "AGENTS.md"])
def test_the_documents_open_with_the_manifest_frontmatter(name):
    m = manifest()
    head = doc(name).split("---", 2)[1]
    assert f"bobbin: {m['name']}" in head
    assert f"provider: {m['provider']['vendor']} {m['provider']['product']}" in head
    assert f"webhook_docs: {m['provider']['webhook_docs']}" in head


@pytest.mark.parametrize("name", ["SETUP.md", "AGENTS.md"])
def test_the_configuration_shown_in_the_documents_expands(name, tmp_path):
    secret = write_secret(tmp_path / "secret")
    text = doc(name)
    (block,) = [b for b in FENCE.findall(text) if "[bobbins." in b]
    block = block.replace("/etc/hookspool/secrets/cloudflare-alerts", secret)
    parsed = tomllib.loads(block)
    assert set(parsed["bobbins"]) == {INSTANCE}
    out = PK.expand(C.merge(C.DEFAULTS, parsed))
    assert INSTANCE in out["sources"]


@pytest.mark.parametrize("name", ["README.md", "SETUP.md", "AGENTS.md"])
def test_no_document_leaves_a_placeholder_or_a_drafting_fragment(name):
    text = doc(name)
    assert "TODO" not in text and "TBD" not in text


def test_the_bobbin_has_no_python_and_calls_nothing_outside_core():
    assert not [f for f in os.listdir(DIR) if f.endswith(".py")]
