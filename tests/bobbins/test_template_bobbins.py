"""The template bobbins trello, github and kaneo (ARCHITECTURE 14.3): each manifest loads with no
errors, a sample instance expands, and every capture in the bobbin's ``captures/`` runs through the
intake's source test (the offline source stages, which never enqueue) with the result its ``# expect:``
line names. Two github instances share one bobbin with their own secrets."""
from __future__ import annotations

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.dirname(HERE)
sys.path.insert(0, TESTS)
sys.path.insert(0, os.path.dirname(TESTS))

import sourcekit  # noqa: E402
from sourcekit import SENTINEL  # noqa: E402

from hookspool import bobbin_loader as PK  # noqa: E402
from hookspool import config as C  # noqa: E402
from hookspool import intake as I  # noqa: E402
from hookspool.plugin import parse_capture  # noqa: E402

NAMES = ("github", "kaneo", "trello")
TEST_SECRET = "test-template-secret-" + SENTINEL       # what every template bobbin's captures are signed with
SAMPLE_INSTANCE = {"github": "github-ci", "kaneo": "kaneo", "trello": "trello"}


@pytest.fixture
def secret_file(tmp_path):
    p = tmp_path / "secret"
    p.write_text(TEST_SECRET)
    os.chmod(p, 0o600)
    return str(p)


def _spec(name: str, inst: str, secret: str) -> dict:
    spec = {"bobbin": name, "secrets": {"secret": {"file": secret}}, "outputs": {"*": f"hermes:{inst}"}}
    if name in sourcekit.SAMPLE_SETTINGS:
        spec["settings"] = dict(sourcekit.SAMPLE_SETTINGS[name])
    return spec


def expanded(instances: dict[str, dict]) -> dict:
    cfg = C.merge(C.DEFAULTS, {"bobbins": instances, "alerts": {"argv": ["true"]}})
    out = PK.expand(cfg)
    for inst in instances:
        if inst in sourcekit.SAMPLE_SETTINGS:
            # the loader does not yet map [settings] into [source.template] (ARCHITECTURE 14.3.2a), so the
            # sample instance's callback URL is carried across here, as the expansion will once it does
            out["sources"][inst] = {**out["sources"][inst], **sourcekit.SAMPLE_SETTINGS[inst]}
    return out


# ---- manifests -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", NAMES)
def test_manifest_loads_with_no_errors(name):
    pkgs, errs = PK.discover(C.merge(C.DEFAULTS, {}))
    assert errs == [], errs
    pkg = pkgs[name]
    assert pkg.errors == [] and pkg.builtin
    assert "template" in pkg.source and "module" not in pkg.source
    assert set(pkg.secrets) == {"secret"}
    assert set(pkg.outputs) == {"*"}


@pytest.mark.parametrize("name", NAMES)
def test_the_bobbin_has_its_required_documents_and_captures(name):
    d = os.path.join(sourcekit.BOBBINS, name)
    for doc in ("README.md", "SETUP.md", "AGENTS.md"):
        assert os.path.isfile(os.path.join(d, doc)), doc
    agents = open(os.path.join(d, "AGENTS.md")).read()
    assert "## Needs from the human" in agents and "\n1. " in agents
    assert "secret" in open(os.path.join(d, "SETUP.md")).read()
    assert sourcekit.captures(name), "captures/*.http is required"


def test_the_example_files_are_pointers_to_the_bobbins():
    for name in NAMES:
        path = os.path.join(sourcekit.ROOT, "examples", "sources", f"{name}.toml")
        with open(path) as f:
            body = [line for line in f if line.strip() and not line.lstrip().startswith("#")]
        assert body == [], f"{path} must hold comments only"
        assert f"hookspool/bobbins/{name}" in open(path).read()


def test_the_capture_set_covers_the_outcomes():
    def expects(name):
        out = {}
        for c in sourcekit.captures(name):
            with open(c, "rb") as f:
                out[os.path.basename(c)] = parse_capture(f.read()).meta["expect"]
        return out
    assert expects("trello") == {"event.http": "queued", "head.http": "challenge", "tampered.http": "unverified"}
    assert expects("github")["event.http"] == "queued"
    assert expects("github")["ping.http"] == "ignored"
    assert "unverified" in expects("github").values()
    assert expects("kaneo") == {"event.http": "queued", "tampered.http": "unverified"}


# ---- a sample instance expands and its captures run through the intake ------------------------------
@pytest.mark.parametrize("name", NAMES)
def test_sample_instance_expands_to_a_template_source(name, secret_file):
    inst = SAMPLE_INSTANCE[name]
    out = expanded({inst: _spec(name, inst, secret_file)})
    src = out["sources"][inst]
    assert src["module"] == "hookspool.sources.template"
    assert src["action"] == f"hermes:{inst}"
    assert src["secrets"] == {"secret": {"file": secret_file}}


def test_github_instances_share_the_bobbin_with_their_own_secrets(tmp_path, secret_file):
    other = tmp_path / "prs"
    other.write_text(TEST_SECRET + "-prs")
    os.chmod(other, 0o600)
    out = expanded({"github-ci": _spec("github", "github-ci", secret_file),
                    "github-prs": _spec("github", "github-prs", str(other))})
    assert out["sources"]["github-ci"]["secrets"]["secret"]["file"] == secret_file
    assert out["sources"]["github-prs"]["secrets"]["secret"]["file"] == str(other)
    assert out["sources"]["github-ci"]["scheme"] == out["sources"]["github-prs"]["scheme"] == "hmac-sha256-hex"


CASES = [(name, cap) for name in NAMES for cap in sourcekit.captures(name)]


@pytest.mark.parametrize("name,cap", CASES, ids=[f"{n}/{os.path.basename(c)}" for n, c in CASES])
def test_each_capture_runs_through_the_intake(name, cap, secret_file):
    inst = SAMPLE_INSTANCE[name]
    cfg = expanded({inst: _spec(name, inst, secret_file)})
    with open(cap, "rb") as f:
        raw = parse_capture(f.read())
    out = I.source_test(cfg, inst, raw)
    assert out["result"] == raw.meta["expect"], out
    assert SENTINEL not in json.dumps(out, default=str)
