"""The template bobbins trello, github and kaneo (ARCHITECTURE 14.3): each manifest loads with no
errors, a sample instance expands and checks clean through the real config path, every capture in the
bobbin's ``captures/`` runs through the intake's source test with the result its ``# expect:`` line
names, and the negative cases refuse what they should (a wrong secret, a missing or mismatched callback
URL, a missing secret, a bad setting). The setup documents are checked against the config they show:
each declared secret and required setting appears in both, every config table in them expands, and
the agent document keeps its placeholders in ANGLE_CASE and asks the person for every credential."""
from __future__ import annotations

import json
import os
import re
import sys
import tomllib

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

NAMES = ("github.repo", "kaneo.projects", "atlassian.trello")
TEST_SECRET = "test-template-secret-" + SENTINEL       # what every template bobbin's captures are signed with
SAMPLE_INSTANCE = {"github.repo": "github-ci", "kaneo.projects": "kaneo", "atlassian.trello": "trello"}
TRELLO = "atlassian.trello"
SAMPLE_CALLBACK = sourcekit.SAMPLE_SETTINGS[TRELLO]["callback_url"]
FENCE = re.compile(r"```toml\n(.*?)```", re.S)
PLACEHOLDER = re.compile(r"<([^<>\s]+)>")
ANGLE_CASE = re.compile(r"^[A-Z][A-Z0-9_]*$")
# names of values a person must provide; an agent document never shows a placeholder for one of these
CREDENTIAL_PLACEHOLDERS = {"SECRET", "API_KEY", "TOKEN", "TRELLO_API_KEY", "TRELLO_TOKEN", "APP_SECRET"}


def write_secret(path, value: str) -> str:
    path.write_text(value)
    os.chmod(path, 0o600)
    return str(path)


@pytest.fixture
def secret_file(tmp_path):
    return write_secret(tmp_path / "secret", TEST_SECRET)


def _spec(name: str, inst: str, secret: str) -> dict:
    spec = {"bobbin": name, "secrets": {"secret": {"file": secret}}, "outputs": {"*": f"hermes:{inst}"}}
    if name in sourcekit.SAMPLE_SETTINGS:
        spec["settings"] = dict(sourcekit.SAMPLE_SETTINGS[name])
    return spec


def expanded(instances: dict[str, dict]) -> dict:
    return PK.expand(C.merge(C.DEFAULTS, {"bobbins": instances, "alerts": {"argv": ["true"]}}))


def _inline(v) -> str:
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{json.dumps(k)} = {_inline(x)}" for k, x in v.items()) + " }"
    return sourcekit.toml_value(v)


def config_file(tmp_path, instances: dict[str, dict]) -> str:
    """The config an operator would write for these instances, as a file ``load_config`` reads."""
    blocks = [f"[bobbins.{inst}]\n" + "\n".join(f"{k} = {_inline(v)}" for k, v in spec.items())
              for inst, spec in instances.items()]
    subs = tmp_path / "subscriptions.json"
    subs.write_text(json.dumps({f"hermes:{inst}".split(":", 1)[1]: {"secret": "s"} for inst in instances}))
    p = tmp_path / "config.toml"
    p.write_text(f'[spool]\nhome = "{tmp_path / "home"}"\n[alerts]\ndry = true\n'
                 f'[hermes]\nsubscriptions_path = "{subs}"\n\n' + "\n\n".join(blocks) + "\n")
    return str(p)


def event_capture(name: str) -> str:
    return os.path.join(sourcekit.bobbin_dir(name), "captures", "event.http")


def doc(name: str, which: str) -> str:
    with open(os.path.join(sourcekit.bobbin_dir(name), which), encoding="utf-8") as f:
        return f.read()


def manifest(name: str) -> dict:
    with open(sourcekit.bobbin_manifest(name), "rb") as f:
        return tomllib.load(f)


def fill(block: str, inst: str) -> str:
    """A documented config table with its placeholders given sample values (14.3.8)."""
    values = {"INSTANCE": inst, "CALLBACK_URL": f"https://hooks.example.test/hooks/{inst}"}

    def sub(m):
        if m.group(1) not in values:
            raise AssertionError(f"no sample value for placeholder <{m.group(1)}>")
        return values[m.group(1)]
    return PLACEHOLDER.sub(sub, block)


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
    d = sourcekit.bobbin_dir(name)
    for doc_name in ("README.md", "SETUP.md", "AGENTS.md"):
        assert os.path.isfile(os.path.join(d, doc_name)), doc_name
    agents = doc(name, "AGENTS.md")
    assert "## Needs from the human" in agents and "\n1. " in agents
    assert sourcekit.captures(name), "captures/*.http is required"


def test_the_setup_documents_are_packaged():
    with open(os.path.join(sourcekit.ROOT, "pyproject.toml"), "rb") as f:
        globs = tomllib.load(f)["tool"]["setuptools"]["package-data"]["hookspool"]
    for want in ("bobbins/*/*/bobbin.toml", "bobbins/*/*/README.md", "bobbins/*/*/SETUP.md", "bobbins/*/*/AGENTS.md",
                 "bobbins/*/*/captures/*.http"):
        assert want in globs, want


def test_the_example_files_are_pointers_to_the_bobbins():
    for name in NAMES:
        path = os.path.join(sourcekit.ROOT, "examples", "sources", f"{sourcekit.SOURCE_OF[name]}.toml")
        with open(path) as f:
            body = [line for line in f if line.strip() and not line.lstrip().startswith("#")]
        assert body == [], f"{path} must hold comments only"
        assert f"hookspool/bobbins/{name.replace('.', '/')}" in open(path).read()


def test_the_capture_set_covers_the_outcomes():
    def expects(name):
        out = {}
        for c in sourcekit.captures(name):
            with open(c, "rb") as f:
                out[os.path.basename(c)] = parse_capture(f.read()).meta["expect"]
        return out
    assert expects(TRELLO) == {"event.http": "queued", "head.http": "challenge", "tampered.http": "unverified"}
    assert expects("github.repo")["event.http"] == "queued"
    assert expects("github.repo")["ping.http"] == "ignored"
    assert "unverified" in expects("github.repo").values()
    assert expects("kaneo.projects") == {"event.http": "queued", "tampered.http": "unverified"}


def test_trello_retries_are_declared_as_the_sender_does_them():
    assert manifest(TRELLO)["source"]["sender_retries"] is True
    assert manifest("github.repo")["source"]["sender_retries"] is False


# ---- a sample instance expands, and checks clean through the real config path ----------------------------
@pytest.mark.parametrize("name", NAMES)
def test_sample_instance_expands_to_a_template_source(name, secret_file):
    inst = SAMPLE_INSTANCE[name]
    out = expanded({inst: _spec(name, inst, secret_file)})
    src = out["sources"][inst]
    assert src["module"] == "hookspool.sources.template"
    assert src["action"] == f"hermes:{inst}"
    assert src["secrets"] == {"secret": {"file": secret_file}}


def test_the_trello_callback_url_reaches_the_source_through_load_config_and_check(tmp_path, secret_file):
    path = config_file(tmp_path, {"trello": _spec(TRELLO, "trello", secret_file)})
    cfg = C.load_config(path)
    assert cfg["sources"]["trello"]["callback_url"] == SAMPLE_CALLBACK
    rep = C.check(cfg)
    assert rep.errors == [], rep.errors


def test_github_instances_share_the_bobbin_with_their_own_secrets(tmp_path, secret_file):
    other = write_secret(tmp_path / "prs", TEST_SECRET + "-prs")
    out = expanded({"github-ci": _spec("github.repo", "github-ci", secret_file),
                    "github-prs": _spec("github.repo", "github-prs", other)})
    assert out["sources"]["github-ci"]["secrets"]["secret"]["file"] == secret_file
    assert out["sources"]["github-prs"]["secrets"]["secret"]["file"] == other
    assert out["sources"]["github-ci"]["scheme"] == out["sources"]["github-prs"]["scheme"] == "hmac-sha256-hex"


# ---- negative cases ------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", NAMES)
def test_a_wrong_secret_refuses_the_signed_delivery(name, tmp_path):
    wrong = write_secret(tmp_path / "wrong", TEST_SECRET + "-wrong")
    inst = SAMPLE_INSTANCE[name]
    cfg = expanded({inst: _spec(name, inst, wrong)})
    with open(event_capture(name), "rb") as f:
        raw = parse_capture(f.read())
    assert raw.meta["expect"] == "queued"
    out = I.source_test(cfg, inst, raw)
    assert out["result"] == "unverified" and out["status"] == 401, out


@pytest.mark.parametrize("name", NAMES)
def test_a_missing_secret_is_an_expansion_error(name, secret_file):
    inst = SAMPLE_INSTANCE[name]
    spec = _spec(name, inst, secret_file)
    del spec["secrets"]
    with pytest.raises(PK.BobbinError) as ei:
        expanded({inst: spec})
    assert any(f"bobbins.{inst}.secrets.secret: required by bobbin {name!r}" in e for e in ei.value.errors)


@pytest.mark.parametrize("name", NAMES)
def test_an_unknown_setting_or_a_setting_of_the_wrong_type_is_an_expansion_error(name, secret_file):
    inst = SAMPLE_INSTANCE[name]
    spec = _spec(name, inst, secret_file)
    spec["settings"] = {**spec.get("settings", {}), "nope": "x"}
    with pytest.raises(PK.BobbinError) as ei:
        expanded({inst: spec})
    assert any(f"bobbins.{inst}.settings.nope: bobbin {name!r} has no such setting" in e for e in ei.value.errors)
    if name == TRELLO:
        spec["settings"] = {"callback_url": 5}
        with pytest.raises(PK.BobbinError) as ei:
            expanded({inst: spec})
        assert any("settings.callback_url: must be a string" in e for e in ei.value.errors)


def test_trello_without_a_callback_url_is_an_expansion_error(secret_file):
    spec = _spec(TRELLO, "trello", secret_file)
    del spec["settings"]
    with pytest.raises(PK.BobbinError) as ei:
        expanded({"trello": spec})
    assert any("bobbins.trello.settings.callback_url: required by bobbin 'atlassian.trello'" in e for e in ei.value.errors)


def test_trello_with_a_mismatched_callback_url_refuses_the_signed_delivery(secret_file):
    spec = _spec(TRELLO, "trello", secret_file)
    spec["settings"] = {"callback_url": "https://hooks.example.test/hooks/other"}
    cfg = expanded({"trello": spec})
    with open(event_capture(TRELLO), "rb") as f:
        raw = parse_capture(f.read())
    out = I.source_test(cfg, "trello", raw)
    assert out["result"] == "unverified", out


# ---- the captures run through the intake ----------------------------------------------------------------
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


# ---- the setup documents match the manifest and the config they show (14.3.8) ----------------------------
@pytest.mark.parametrize("name", NAMES)
def test_each_declared_secret_and_required_setting_is_in_both_documents(name):
    m = manifest(name)
    for which in ("SETUP.md", "AGENTS.md"):
        tables = "\n".join(FENCE.findall(doc(name, which)))
        for s in m.get("secrets", {}):
            assert f"secrets.{s} =" in tables, f"{which} shows no config for secret {s}"
        for s, d in m.get("settings", {}).items():
            if d.get("required") is True:
                assert f"{s} =" in tables, f"{which} shows no config for required setting {s}"


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("which", ["SETUP.md", "AGENTS.md"])
def test_the_config_tables_in_the_documents_expand(name, which):
    text = doc(name, which)
    blocks = [b for b in FENCE.findall(text) if "[bobbins." in b]
    assert blocks, f"{which} shows a config table"
    for b in blocks:
        parsed = tomllib.loads(fill(b, SAMPLE_INSTANCE[name]))
        out = PK.expand(C.merge(C.DEFAULTS, parsed))
        assert set(parsed["bobbins"]) <= set(out["sources"])


@pytest.mark.parametrize("name", NAMES)
def test_agents_placeholders_are_angle_case_and_no_credential_is_a_placeholder(name):
    names = PLACEHOLDER.findall(doc(name, "AGENTS.md"))
    bad = [n for n in names if not ANGLE_CASE.match(n)]
    assert bad == [], f"placeholders must be <ANGLE_CASE>: {bad}"
    assert not CREDENTIAL_PLACEHOLDERS & set(names), "an agent is never given a secret value to handle"


def test_trello_agents_asks_the_person_for_the_api_key_and_token():
    needs = doc("trello", "AGENTS.md").split("## Needs from the human", 1)[1].split("## Steps", 1)[0]
    assert "API key" in needs and "token" in needs
    assert "/1/webhooks" in doc("trello", "AGENTS.md")


@pytest.mark.parametrize("name", NAMES)
def test_agents_marks_each_provider_change_outward(name):
    steps = doc(name, "AGENTS.md").split("## Steps", 1)[1]
    assert "**Outward" in steps, "a step that changes the provider is marked outward"


def test_github_sweep_is_a_concrete_recipe():
    for which in ("SETUP.md", "AGENTS.md"):
        text = doc("github", which)
        assert "deliveries" in text and "attempts" in text, which
        assert "the reason none is needed" not in text and "(daily, or" not in text, which
    assert "gh api" in doc("github", "AGENTS.md")


def test_no_document_leaves_a_drafting_fragment():
    for name in NAMES:
        for which in ("README.md", "SETUP.md", "AGENTS.md"):
            text = doc(name, which)
            assert "Known gap" not in text and "known gap" not in text, (name, which)
            assert "TODO" not in text and "TBD" not in text, (name, which)
