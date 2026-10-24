"""The ``gmail`` and ``gcal`` bobbins as units of a deployment (ARCHITECTURE 14.3): their manifests, how an instance
expands (the processor's destination, its environment, the optional credential), that the outputs the code can
produce are the outputs the manifest declares, and that the documents every bobbin ships (14.3.8) name every
secret and required setting and show a configuration that expands."""
from __future__ import annotations

import json
import os
import re

import pytest

from hookspool import bobbin_loader as PK
from hookspool import config as C
from hookspool.bobbins.google.calendar import processor as gcal_proc
from hookspool.bobbins.google.gmail import processor as gmail_proc

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BOBBINS = os.path.join(ROOT, "hookspool", "bobbins")
NAMES = ["google.gmail", "google.calendar"]
INSTANCE = {"google.gmail": "gmail", "google.calendar": "calendar"}     # the docs' instance names
DOCS = ["README.md", "SETUP.md", "AGENTS.md"]


def package(name):
    pkgs, errs = PK.discover({})
    assert errs == [], errs
    return pkgs[name]


@pytest.mark.parametrize("name", NAMES)
def test_the_manifest_is_valid_and_complete(name):
    pkg = package(name)
    assert pkg.errors == [] and pkg.builtin and pkg.processor is not None and pkg.processor.get("state") is True
    for f in [*DOCS, "bobbin.toml", "source.py", "processor.py", "__init__.py"]:
        assert os.path.isfile(os.path.join(BOBBINS, *name.split("."), f)), f
    assert [f for f in os.listdir(os.path.join(BOBBINS, *name.split("."), "captures")) if f.endswith(".http")]


def test_the_outputs_the_code_can_produce_are_the_declared_outputs():
    assert set(gmail_proc.OUTPUTS) == set(package("google.gmail").outputs)
    assert set(gcal_proc.OUTPUTS) == set(package("google.calendar").outputs)


def test_the_credentials_are_processor_secrets_and_the_agents_is_optional():
    g, c = package("google.gmail").secrets, package("google.calendar").secrets
    assert "processor" not in g["push_token"] and g["cred_owner"]["processor"] is True
    assert g["cred_agent"]["processor"] is True and g["cred_agent"]["required"] is False
    assert c["cred"]["processor"] is True and "processor" not in c["channel_token"]
    assert package("google.gmail").settings["hooks"]["type"] == "path" and package("google.calendar").settings["hooks"]["type"] == "path"
    assert package("google.gmail").settings["owner"]["required"] is True


def secret_files(tmp_path, *names):
    out = {}
    for n in names:
        p = tmp_path / f"{n}.secret"
        p.write_text("test-" + n + "\n")
        os.chmod(p, 0o600)
        out[n] = str(p)
    return out


def load(tmp_path, body):
    p = tmp_path / "config.toml"
    p.write_text(f'[spool]\nhome = "{tmp_path / "home"}"\n[alerts]\ndry = true\n' + body)
    return C.load_config(str(p))


def test_a_gmail_instance_expands_to_a_source_and_a_processor_destination(tmp_path):
    s = secret_files(tmp_path, "tok", "own", "ast")
    cfg = load(tmp_path, f'''
[bobbins.mail]
bobbin = "google.gmail"
secrets.push_token = {{ file = "{s['tok']}" }}
secrets.cred_owner = {{ file = "{s['own']}" }}
secrets.cred_agent = {{ file = "{s['ast']}" }}
settings = {{ owner = "owner@example.test", agent = "agent@example.test", skip_keywords = ["sale"] }}
outputs = {{ scheduling = "hermes:a", mailbox-changed = "hermes:b" }}
expect = true
[hermes]
subscriptions_path = "{tmp_path / 'subs.json'}"
''')
    src, dst = cfg["sources"]["mail"], cfg["destinations"]["script:mail"]
    assert src["module"] == "hookspool.bobbins.google.gmail.source" and src["action"] == "script:mail"
    assert src["span"] == "mail" and src["expect_every_s"] == 345600
    assert dst["processor"] == "mail" and dst["max_inflight"] == 1 and dst["state_dir"].endswith("bobbins/mail")
    env = dst["env"]
    assert json.loads(env["HOOKSPOOL_SECRET_REFS"]) == {"cred_owner": {"file": s["own"]}, "cred_agent": {"file": s["ast"]}}
    settings = json.loads(env["HOOKSPOOL_SETTINGS"])
    assert settings["owner"] == "owner@example.test" and settings["skip_keywords"] == ["sale"]
    assert settings["hours"] == "9:00-17:30" and settings["api_url"].startswith("https://gmail.googleapis.com/")
    assert json.loads(env["HOOKSPOOL_DELTA_ROUTES"]) == {"scheduling": "hermes:a", "mailbox-changed": "hermes:b"}
    assert "owner@example.test" not in json.dumps(src), "settings are the processor's, not the source's"


def test_a_gmail_instance_without_an_assistant_needs_no_assistant_credential(tmp_path):
    s = secret_files(tmp_path, "tok", "own")
    cfg = load(tmp_path, f'''
[bobbins.gmail]
bobbin = "google.gmail"
secrets.push_token = {{ file = "{s['tok']}" }}
secrets.cred_owner = {{ file = "{s['own']}" }}
settings = {{ owner = "owner@example.test" }}
outputs = {{ scheduling = "hermes:a" }}
''')
    assert json.loads(cfg["destinations"]["script:gmail"]["env"]["HOOKSPOOL_SECRET_REFS"]) == {"cred_owner": {"file": s["own"]}}


def test_a_gcal_instance_expands_and_check_accepts_a_script_destination_for_changed(tmp_path):
    s = secret_files(tmp_path, "tok", "cred")
    cfg = load(tmp_path, f'''
[bobbins.gcal]
bobbin = "google.calendar"
secrets.channel_token = {{ file = "{s['tok']}" }}
secrets.cred = {{ file = "{s['cred']}" }}
settings = {{ tz = "America/New_York", own = ["a@example.test"] }}
outputs = {{ conflict = "hermes:c", changed = "script:refresh" }}
expect = true
[destinations."script:refresh"]
argv = ["/bin/true"]
''')
    assert cfg["sources"]["gcal"]["expect_every_s"] == 172800 and cfg["sources"]["gcal"]["span"] == "calendar"
    rep = C.check(cfg, check_secrets=True)
    assert [e for e in rep.errors if "gcal" in e] == [], rep.errors


def test_a_missing_required_secret_or_setting_is_refused(tmp_path):
    s = secret_files(tmp_path, "tok", "own")
    body = f'''
[bobbins.gmail]
bobbin = "google.gmail"
secrets.push_token = {{ file = "{s['tok']}" }}
settings = {{ owner = "owner@example.test" }}
outputs = {{ scheduling = "hermes:a" }}
'''
    with pytest.raises(PK.BobbinError) as e:
        load(tmp_path, body)
    assert any("secrets.cred_owner: required" in m for m in e.value.errors)
    with pytest.raises(PK.BobbinError) as e:
        load(tmp_path, body.replace('settings = { owner = "owner@example.test" }', "settings = {}")
             + f'secrets.cred_owner = {{ file = "{s["own"]}" }}\n')
    assert any("settings.owner: required" in m for m in e.value.errors)


def test_check_refuses_an_env_reference_for_a_credential(tmp_path):
    s = secret_files(tmp_path, "tok")
    cfg = load(tmp_path, f'''
[bobbins.gmail]
bobbin = "google.gmail"
secrets.push_token = {{ file = "{s['tok']}" }}
secrets.cred_owner = {{ env = "GOOGLE_CRED" }}
settings = {{ owner = "owner@example.test" }}
outputs = {{ scheduling = "hermes:a" }}
''')
    rep = C.check(cfg)
    assert any("cred_owner" in e and "does not inherit" in e for e in rep.errors), rep.errors


# ---- the documents (14.3.8) -------------------------------------------------------------------------------------
def read(name, doc):
    with open(os.path.join(BOBBINS, *name.split("."), doc), encoding="utf-8") as f:
        return f.read()


@pytest.mark.parametrize("name", NAMES)
def test_every_secret_and_required_setting_appears_in_the_setup_documents(name):
    pkg = package(name)
    needed = list(pkg.secrets) + [k for k, v in pkg.settings.items() if v.get("required") is True]
    for doc in ("SETUP.md", "AGENTS.md"):
        text = read(name, doc)
        for n in needed:
            assert f"{n}" in text, f"{name}/{doc} does not mention {n}"


@pytest.mark.parametrize("name", NAMES)
def test_the_agent_document_has_the_needs_list_numbered_steps_and_marks_outward_steps(name):
    text = read(name, "AGENTS.md")
    assert "## Needs from the human" in text
    steps = re.findall(r"^(\d+)\. \*\*", text, re.M)
    assert steps == [str(i) for i in range(1, len(steps) + 1)] and len(steps) >= 5
    assert text.count("`outward`") >= 3 and "<ANGLE_CASE>" not in text
    for placeholder in re.findall(r"<([^>\s]+)>", text):
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", placeholder) or placeholder in ("...",), placeholder


PLACEHOLDERS = {"SECRETS_DIR", "CRED_OWNER_FILE", "CRED_AGENT_FILE", "CRED_FILE", "OWNER_ADDRESS", "AGENT_ADDRESS",
                "TIME_ZONE", "DESTINATION"}


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("doc", ["SETUP.md", "AGENTS.md"])
def test_the_configuration_shown_in_the_document_expands(name, doc, tmp_path):
    text = read(name, doc)
    blocks = [b for b in re.findall(r"```toml\n(.*?)```", text, re.S) if f"[bobbins.{INSTANCE[name]}]" in b]
    assert len(blocks) == 1
    block = blocks[0]
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    for fn in ("gmail_push_token", "calendar_channel_token", "owner.json", "assistant.json"):
        p = secrets_dir / fn
        p.write_text("x")
        os.chmod(p, 0o600)
    subs = {"SECRETS_DIR": str(secrets_dir), "CRED_OWNER_FILE": str(secrets_dir / "owner.json"),
            "CRED_AGENT_FILE": str(secrets_dir / "assistant.json"), "CRED_FILE": str(secrets_dir / "owner.json"),
            "OWNER_ADDRESS": "owner@example.test", "AGENT_ADDRESS": "agent@example.test",
            "TIME_ZONE": "America/New_York", "DESTINATION": "hermes:out"}
    for k, v in subs.items():
        block = block.replace(f"<{k}>", v)
    block = block.replace("/etc/hookspool/secrets", str(secrets_dir)).replace("/etc/hookspool/google", str(secrets_dir))
    block = block.replace("/var/lib/briefs", str(tmp_path / "briefs")).replace("/etc/hookspool/calendar_hooks.py", "")
    assert not re.search(r"<[A-Z_]+>", block), block
    extra = '\n[destinations."script:calendar-refresh"]\nargv = ["/bin/true"]\n' if "calendar-refresh" in block else ""
    cfg = load(tmp_path, block + extra + f'\n[hermes]\nsubscriptions_path = "{tmp_path / "subs.json"}"\n')
    assert INSTANCE[name] in cfg["sources"] and f"script:{INSTANCE[name]}" in cfg["destinations"]
