"""The Hermes migration guide and the AGENTS.md section that summarises it, against the code.

Every command, option and config key they name exists; each fenced toml block in the guide parses; and the guide's
configs, combined with a fixture subscriptions file, pass `check` (and fail it where the guide says they do).
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tomllib

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_agents_md import check_agents_commands  # noqa: E402

from hookspool import config as hs_config  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUIDE = os.path.join(ROOT, "docs", "getting-started", "from-hermes-webhooks.md")
FIX = os.path.join(ROOT, "tests", "fixtures", "hermes_migration")
SECRETS = "/etc/hookspool/secrets/"


def read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def agents_section() -> str:
    text = read(os.path.join(ROOT, "AGENTS.md"))
    m = re.search(r"^### Moving a Hermes setup to hookspool\s*$", text, re.M)
    assert m, "AGENTS.md has no 'Moving a Hermes setup to hookspool' section"
    nxt = re.search(r"^#{1,3} ", text[m.end():], re.M)
    return text[m.end():m.end() + nxt.start()]


def toml_blocks() -> list[str]:
    return re.findall(r"```toml\n(.*?)```", read(GUIDE), re.S)


def block_with(marker: str) -> str:
    hits = [b for b in toml_blocks() if marker in b]
    assert len(hits) == 1, (marker, len(hits))
    return hits[0]


def test_commands_and_options_in_the_guide_and_the_agents_section_are_real():
    seen = check_agents_commands(read(GUIDE))
    assert {"check", "reload", "list", "bobbin", "enqueue"} <= seen
    seen = check_agents_commands(agents_section())
    assert {"check", "reload", "list", "bobbin"} <= seen


def test_agents_section_is_a_set_of_agent_steps_that_links_the_guide():
    s = agents_section()
    assert "docs/getting-started/from-hermes-webhooks.md" in s
    for word in ("Needs from the human", "Rollback", "Check:", "Outward"):
        assert word in s, word
    assert len(re.findall(r"^\d\. ", s, re.M)) >= 5
    assert os.path.exists(GUIDE)


def test_every_config_key_the_agents_section_and_the_guide_name_exists():
    """`[hermes]` keys are checked here; instance, destination and route keys by loading the guide's blocks below
    (an unknown key is an error in `check`)."""
    assert {"base_url", "subscriptions_path"} <= hs_config._SECTION_KEYS["hermes"]
    text = agents_section() + read(GUIDE)
    for key in ("base_url", "subscriptions_path", "unwrap", "span", "outputs", "event_header", "tee"):
        assert re.search(rf"\b{key}\b", text), key


def test_every_toml_block_in_the_guide_parses():
    blocks = toml_blocks()
    assert len(blocks) >= 5
    for b in blocks:
        tomllib.loads(b)


@pytest.fixture
def home(tmp_path):
    """A scratch layout for the guide's config: secret files where the guide puts them, the fixture subscriptions."""
    sec = tmp_path / "secrets"
    sec.mkdir()
    for name in ("github-ci", "invoices"):
        f = sec / name
        f.write_text("test-secret-" + name)
        f.chmod(0o600)
    return tmp_path


def load_and_check(home, subs: str, *blocks: str):
    subs_path = home / "subscriptions.json"
    shutil.copy(os.path.join(FIX, subs), subs_path)
    body = "\n".join(blocks).replace(SECRETS, str(home / "secrets") + "/")
    body = re.sub(r'subscriptions_path = "[^"]*"', f'subscriptions_path = "{subs_path}"', body)
    if "subscriptions_path" not in body:
        body = f'[hermes]\nsubscriptions_path = "{subs_path}"\n' + body
    cfg_path = home / "config.toml"
    cfg_path.write_text(f'[spool]\nhome = "{home / "spool"}"\n[alerts]\ndry = true\n' + body)
    cfg = hs_config.load_config(str(cfg_path))
    return cfg, hs_config.check(cfg)


def test_the_first_pass_configs_pass_check_against_routes_that_filter_events(home):
    hermes = block_with("[hermes]")
    github = block_with("[bobbins.github-ci]\nbobbin = \"github.repo\"\nsecrets.secret = { file = \"/etc/hookspool/secrets/github-ci\" }\nspan = \"immediate\"")
    invoices = block_with("[bobbins.invoices]")
    cfg, rep = load_and_check(home, "subscriptions_before.json", hermes, github, invoices)
    assert rep.errors == [], rep.errors
    # unwrap is what lets the gateway keep its events filter, and the routes deliver one event per batch
    assert cfg["destinations"]["hermes:github-ci"]["unwrap"] is True
    assert cfg["destinations"]["hermes:invoices"]["event_header"] == "X-Invoices-Event"
    assert not [w for w in rep.warnings if "coalesces" in w], rep.warnings


def test_the_coalescing_configs_need_the_gateway_filters_removed(home):
    hermes = block_with("[hermes]")
    batch = block_with('span = "burst"')
    wake = block_with("github-ci-wake")
    # after the second step: the route no longer filters or coalesces, so check is clean
    cfg, rep = load_and_check(home, "subscriptions_after.json", hermes, batch, wake)
    assert rep.errors == [], rep.errors
    assert "unwrap" not in cfg["destinations"].get("hermes:github-ci", {})
    assert cfg["destinations"]["script:wake-ci"]["argv"][0].endswith("run-job")
    # before it: check refuses, as the guide says
    _, rep = load_and_check(home, "subscriptions_before.json", hermes, batch, wake)
    assert any("hermes:github-ci" in e and "'events'" in e for e in rep.errors), rep.errors


def test_the_tee_route_is_a_copy_not_a_second_primary(home):
    hermes = block_with("[hermes]")
    github = block_with("span = \"immediate\"\noutputs = { \"*\" = \"hermes:github-ci\" }\n\n[destinations")
    wake = block_with("github-ci-wake")
    cfg, rep = load_and_check(home, "subscriptions_before.json", hermes, github, wake)
    assert rep.errors == [], rep.errors
    route = next(r for r in cfg["routes"] if r["name"] == "github-ci-wake")
    assert route["tee"] is True and route["source"] == "github-ci"
