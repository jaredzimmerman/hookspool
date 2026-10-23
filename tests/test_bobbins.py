"""Bobbins (ARCHITECTURE §14.3): manifests, discovery, expansion, the check rules, the
CLI, reload through the intake and the dispatcher, and the script destination's ``env``."""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tomllib

import pytest

from conftest import SENTINEL
from hookspool import config as C
from hookspool import bobbin_loader as PK
from hookspool.__main__ import main
from hookspool.destinations.base import DeliveryContext
from hookspool.destinations.script import ScriptDestination

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "bobbins")


# ---- helpers ---------------------------------------------------------------------------------------------
@pytest.fixture
def pkgroot(tmp_path):
    """A private copy of the fixture packages (mode 0755 dirs, 0644 files, owned by this user)."""
    root = tmp_path / "pkgs"
    shutil.copytree(FIXTURES, root)
    for d, dirs, files in os.walk(root):
        os.chmod(d, 0o755)
        for f in files:
            os.chmod(os.path.join(d, f), 0o644)
    os.chmod(tmp_path, 0o755)
    return root


@pytest.fixture
def no_builtins(tmp_path, monkeypatch):
    d = tmp_path / "builtins"
    d.mkdir()
    monkeypatch.setattr(PK, "BUILTIN_DIR", str(d))
    return d


@pytest.fixture
def secrets(tmp_path):
    out = {}
    for n in ("tok", "cred", "sig"):
        p = tmp_path / f"{n}.secret"
        p.write_text(SENTINEL + "-" + n + "\n")
        os.chmod(p, 0o600)
        out[n] = str(p)
    return out


def make_pkg(root, dirname, manifest: str, files: dict | None = None):
    d = root / dirname
    d.mkdir(parents=True)
    (d / "bobbin.toml").write_text(manifest)
    for rel, text in (files or {}).items():
        (d / rel).write_text(text)
    os.chmod(d, 0o755)
    return d


MINIMAL = """
name = "{name}"
version = "1"
[source]
template = {{ scheme = "url-token", key = "id" }}
[secrets.secret]
description = "x"
[outputs."*"]
description = "all"
"""


def manifest_errors(m: str, dirname: str = "pkg") -> list[str]:
    return PK.validate_manifest(tomllib.loads(m), dirname, C.DEFAULTS["spans"])


def cfg_text(tmp_path, pkgroot, body: str) -> str:
    return f"""
[spool]
home = "{tmp_path / 'home'}"
bobbin_path = ["{pkgroot}"]
[alerts]
dry = true
{body}
"""


def load(tmp_path, pkgroot, body: str) -> dict:
    p = tmp_path / "config.toml"
    p.write_text(cfg_text(tmp_path, pkgroot, body))
    return C.load_config(str(p))


def expand_errors(tmp_path, pkgroot, body: str) -> list[str]:
    with pytest.raises(PK.BobbinError) as ei:
        load(tmp_path, pkgroot, body)
    return ei.value.errors


def has(errors, *parts) -> bool:
    return any(all(p in e for p in parts) for e in errors)


def digest_body(secrets, extra: str = "", **over) -> str:
    owner = over.get("owner", '"someone@example.com"')
    return f"""
[bobbins.mail]
bobbin = "digest"
secrets.token = {{ file = "{secrets['tok']}" }}
secrets.cred = {{ file = "{secrets['cred']}" }}
settings = {{ owner = {owner} }}
outputs = {{ summary = "hermes:mail-summary", alert = "hermes:mail-alert" }}
expect = true
{extra}
"""


def relay_body(secrets, extra: str = "") -> str:
    return f"""
[bobbins.relay]
secrets.secret = {{ file = "{secrets['sig']}" }}
outputs = {{ "*" = "script:sink" }}
{extra}
[destinations."script:sink"]
argv = ["/bin/true"]
"""


# ---- manifests (14.3.2) ----------------------------------------------------------------------------------
def test_a_valid_manifest_has_no_errors():
    assert manifest_errors(MINIMAL.format(name="pkg")) == []
    with open(os.path.join(FIXTURES, "digest", "bobbin.toml"), "rb") as f:
        assert PK.validate_manifest(tomllib.load(f), "digest", C.DEFAULTS["spans"]) == []


@pytest.mark.parametrize("edit,expect", [
    (lambda m: m + "\nbogus = 1\n", "bogus: unknown key"),
    (lambda m: m.replace('version = "1"', 'version = "1"\n[extra]\nx = 1'), "extra: unknown key"),
    (lambda m: m.replace('scheme = "url-token"', 'scheme = "url-token", module = "x"'), "template.module"),
    (lambda m: m.replace("[source]", "[source]\nmodule = \"source\""), "exactly one of module or template"),
    (lambda m: m.replace('template = { scheme = "url-token", key = "id" }', 'span = "burst"'),
     "exactly one of module or template"),
    (lambda m: m.replace("[source]", "[source]\nspan = \"glacial\""), "not a span preset"),
    (lambda m: m.replace("[source]", "[source]\nweird = 1"), "source.weird: unknown key"),
    (lambda m: m.replace('description = "x"', 'description = "x"\nrequired = true'), "secrets.secret.required"),
    (lambda m: m + '\n[settings.a]\ndefault = "x"\n', "settings.a.type: required"),
    (lambda m: m + '\n[settings.a]\ntype = "float"\ndefault = 1.0\n', "is not one of"),
    (lambda m: m + '\n[settings.a]\ntype = "string"\n', "settings.a.default: required"),
    (lambda m: m + '\n[settings.a]\ntype = "string"\ndefault = 3\n', "settings.a.default: must be a string"),
    (lambda m: m + '\n[settings.a]\ntype = "int"\ndefault = true\n', "settings.a.default: must be an integer"),
    (lambda m: m + '\n[settings.a]\ntype = "bool"\ndefault = "no"\n', "settings.a.default: must be true or false"),
    (lambda m: m + '\n[settings.a]\ntype = "list"\ndefault = "x"\n', "settings.a.default: must be a list"),
    (lambda m: m + '\n[settings.a]\ntype = "path"\ndefault = "rel/x.py"\n', "settings.a.default: must be an absolute"),
    (lambda m: m + '\n[settings.a]\ntype = "int"\ndefault = 1\nmin = 0\n', "settings.a.min: unknown key"),
    (lambda m: m + '\n[settings.hooks]\ntype = "string"\ndefault = ""\n', "its type must be \"path\""),
    (lambda m: m + '\n[outputs."has space"]\n', "not an event-type token"),
    (lambda m: m + '\n[outputs.ok]\nweight = 1\n', "outputs.ok.weight: unknown key"),
    (lambda m: m.replace('[outputs."*"]\ndescription = "all"', ""), "outputs: at least one output"),
    (lambda m: m + '\n[processor]\nmodule = "other"\n', "processor.module"),
    (lambda m: m + '\n[processor]\nstate = "yes"\n', "processor.state"),
    (lambda m: m + '\n[processor]\nretries = 3\n', "processor.retries: unknown key"),
    (lambda m: m.replace("[source]", "[source]\nsender_retries = 1"), "source.sender_retries"),
    (lambda m: m.replace('[outputs."*"]\ndescription = "all"', "[outputs.a]\n[outputs.b]"),
     'several outputs and no processor must declare "*"'),
])
def test_every_manifest_error_rule(edit, expect):
    errs = manifest_errors(edit(MINIMAL.format(name="pkg")))
    assert has(errs, expect), errs


def test_manifest_name_must_match_its_directory_and_the_name_pattern():
    assert has(manifest_errors(MINIMAL.format(name="pkg"), "other"), "does not match its directory")
    assert has(manifest_errors(MINIMAL.format(name="Pkg"), "Pkg"), "must match")
    assert has(manifest_errors(MINIMAL.format(name="a_b"), "a_b"), "must match")
    # a name with '-' may live in a directory with '_' (its module name)
    assert manifest_errors(MINIMAL.format(name="a-b"), "a_b") == []


def test_a_required_setting_needs_no_default_and_a_missing_local_module_is_an_error(tmp_path):
    m = MINIMAL.format(name="pkg") + '\n[settings.who]\ntype = "string"\nrequired = true\n'
    assert manifest_errors(m) == []
    root = tmp_path / "r"
    make_pkg(root, "pkg", m.replace('template = { scheme = "url-token", key = "id" }', 'module = "source"'))
    pkg = PK.load_package(str(root / "pkg"), str(root), C.DEFAULTS["spans"])
    assert has(pkg.errors, "source.py is not in the package")


# ---- discovery: names, shadowing, '-'/'_' ----------------------------------------------------------------
def test_discovery_finds_builtins_then_path_packages(pkgroot, no_builtins):
    make_pkg(no_builtins, "core", MINIMAL.format(name="core"))
    pkgs, errs = PK.discover({"spool": {"bobbin_path": [str(pkgroot)]}})
    assert errs == []
    assert list(pkgs) == ["core", "digest", "fanout", "relay"]
    assert pkgs["core"].origin == PK.BUILTIN and pkgs["relay"].origin == str(pkgroot)


def test_a_path_package_may_not_shadow_a_builtin(pkgroot, no_builtins, tmp_path, secrets):
    make_pkg(no_builtins, "relay", MINIMAL.format(name="relay"))
    pkgs, errs = PK.discover({"spool": {"bobbin_path": [str(pkgroot)]}})
    assert has(errs, "shadows the built-in package 'relay'")
    assert pkgs["relay"].origin == PK.BUILTIN
    assert has(expand_errors(tmp_path, pkgroot, relay_body(secrets)), "shadows the built-in")


def test_names_that_differ_only_by_dash_and_underscore_are_refused(tmp_path, no_builtins):
    root = tmp_path / "r"
    make_pkg(root, "a-b", MINIMAL.format(name="a-b"))
    make_pkg(root, "a_b", MINIMAL.format(name="a-b"))
    _, errs = PK.discover({"spool": {"bobbin_path": [str(root)]}})
    assert has(errs, "differ only by '-'/'_'")
    root2 = tmp_path / "r2"
    make_pkg(root2, "x-y", MINIMAL.format(name="x-y"))
    make_pkg(root2, "x_y", MINIMAL.format(name="x_y"))       # an invalid name, and the same module name
    _, errs = PK.discover({"spool": {"bobbin_path": [str(root2)]}})
    assert has(errs, "only by '-'/'_'")


def test_the_same_name_in_two_path_directories_is_refused(tmp_path, no_builtins):
    a, b = tmp_path / "a", tmp_path / "b"
    make_pkg(a, "dup", MINIMAL.format(name="dup"))
    make_pkg(b, "dup", MINIMAL.format(name="dup"))
    pkgs, errs = PK.discover({"spool": {"bobbin_path": [str(a), str(b)]}})
    assert has(errs, "already provided by") and pkgs["dup"].origin == str(a)


def test_spool_bobbin_path_errors(tmp_path, no_builtins):
    for body, want in (('[spool]\nbobbin_path = "x"\n', "must be a list"),
                       ('[spool]\nbobbin_path = ["rel"]\n', "must be an absolute path"),
                       (f'[spool]\nbobbin_path = ["{tmp_path / "missing"}"]\n[bobbins.x]\n', "is not a directory")):
        p = tmp_path / "c.toml"
        p.write_text(body)
        with pytest.raises(PK.BobbinError) as ei:
            C.load_config(str(p))
        assert has(ei.value.errors, want), (body, ei.value.errors)


# ---- expansion (14.3.3) ----------------------------------------------------------------------------------
def test_processor_instance_expands_to_a_source_and_a_processor_destination(tmp_path, pkgroot, no_builtins,
                                                                             secrets):
    cfg = load(tmp_path, pkgroot, digest_body(secrets))
    src = cfg["sources"]["mail"]
    assert src["module"] == "_hookspool_bobbins.digest.source"
    assert src["action"] == "script:mail" and src["span"] == "mail" and src["expect_every_s"] == 86400
    assert src["secrets"] == {"token": {"file": secrets["tok"]}, "cred": {"file": secrets["cred"]}}
    d = cfg["destinations"]["script:mail"]
    assert d["processor"] == "mail" and "argv" not in d and d["max_inflight"] == 1 and d["timeout_s"] == 120
    assert d["state_dir"] == str(tmp_path / "home" / "services" / "mail")
    env = d["env"]
    assert env["HOOKSPOOL_BOBBIN"] == "digest" and env["HOOKSPOOL_BOBBIN_INSTANCE"] == "mail"
    assert env["HOOKSPOOL_BOBBIN_DIR"] == str(pkgroot / "digest")
    assert json.loads(env["HOOKSPOOL_DELTA_ROUTES"]) == {"summary": "hermes:mail-summary", "alert": "hermes:mail-alert"}
    assert json.loads(env["HOOKSPOOL_SECRET_REFS"]) == {"cred": {"file": secrets["cred"]}}
    assert cfg["routes"] == []
    assert cfg["_bobbins"]["mail"]["processor"] is True


def test_settings_are_typed_with_defaults_applied(tmp_path, pkgroot, no_builtins, secrets):
    cfg = load(tmp_path, pkgroot, digest_body(secrets))
    s = json.loads(cfg["destinations"]["script:mail"]["env"]["HOOKSPOOL_SETTINGS"])
    assert s == {"owner": "someone@example.com", "limit": 25, "verbose": False, "labels": ["inbox"], "hooks": ""}
    body = digest_body(secrets).replace('settings = { owner = "someone@example.com" }',
                                        'settings = { owner = "o", limit = 5, verbose = true, labels = [] }')
    s = json.loads(load(tmp_path, pkgroot, body)["destinations"]["script:mail"]["env"]["HOOKSPOOL_SETTINGS"])
    assert s["limit"] == 5 and s["verbose"] is True and s["labels"] == []


@pytest.mark.parametrize("settings,want", [
    ('{ limit = 5 }', "settings.owner: required"),
    ('{ owner = 3 }', "settings.owner: must be a string"),
    ('{ owner = "o", limit = "5" }', "settings.limit: must be an integer"),
    ('{ owner = "o", limit = 1.5 }', "settings.limit: must be an integer"),
    ('{ owner = "o", verbose = 1 }', "settings.verbose: must be true or false"),
    ('{ owner = "o", labels = "x" }', "settings.labels: must be a list"),
    ('{ owner = "o", hooks = "rel/hooks.py" }', "settings.hooks: must be an absolute path"),
    ('{ owner = "o", color = "red" }', "settings.color: package 'digest' has no such setting"),
])
def test_setting_errors(tmp_path, pkgroot, no_builtins, secrets, settings, want):
    body = digest_body(secrets).replace('settings = { owner = "someone@example.com" }', f"settings = {settings}")
    assert has(expand_errors(tmp_path, pkgroot, body), want)


@pytest.mark.parametrize("edit,want", [
    (lambda b: b.replace('bobbin = "digest"', 'bobbin = "nope"'), "unknown package 'nope'"),
    (lambda b: b + "colour = 1\n", "bobbins.mail.colour: unknown key"),
    (lambda b: b.replace("secrets.cred", "secrets.other"), "secrets.cred: required"),
    (lambda b: b.replace("secrets.cred", "secrets.other"), "secrets.other: package 'digest' has no such secret"),
    (lambda b: b.replace('summary = "hermes:mail-summary"', 'digest = "hermes:x"'), "outputs.digest: package"),
    (lambda b: b.replace('summary = "hermes:mail-summary"', "summary = 3"), "outputs.summary: must be a destination"),
    (lambda b: b.replace("expect = true", "expect = 1"), "expect: must be true or false"),
])
def test_instance_errors(tmp_path, pkgroot, no_builtins, secrets, edit, want):
    assert has(expand_errors(tmp_path, pkgroot, edit(digest_body(secrets))), want)


def test_instance_overrides_and_processor_only_keys(tmp_path, pkgroot, no_builtins, secrets):
    body = digest_body(secrets, 'timeout_s = 30\nstate_dir = "/var/tmp/keep"\nquiet_s = 1\nmax_s = 9\n'
                                'dedup_window_s = 60\nspan = "burst"\nexpect_every_s = 99\n')
    cfg = load(tmp_path, pkgroot, body)
    s, d = cfg["sources"]["mail"], cfg["destinations"]["script:mail"]
    assert (s["quiet_s"], s["max_s"], s["dedup_window_s"], s["span"], s["expect_every_s"]) == (1, 9, 60, "burst", 99)
    assert d["timeout_s"] == 30 and d["state_dir"] == "/var/tmp/keep"
    errs = expand_errors(tmp_path, pkgroot, relay_body(secrets, 'timeout_s = 3\nstate_dir = "/x"\n'))
    assert has(errs, "relay.timeout_s: only for a package with a processor")
    assert has(errs, "relay.state_dir: only for a package with a processor")


def test_expect_without_a_recommendation_is_an_error(tmp_path, pkgroot, no_builtins, secrets):
    body = f"""
[bobbins.fan]
bobbin = "fanout"
secrets.secret = {{ file = "{secrets['sig']}" }}
outputs = {{ "*" = "hermes:all" }}
expect = true
"""
    assert has(expand_errors(tmp_path, pkgroot, body), "recommends no expect_every_s")
    cfg = load(tmp_path, pkgroot, body.replace("expect = true", "expect = true\nexpect_every_s = 60"))
    assert cfg["sources"]["fan"]["expect_every_s"] == 60


# ---- outputs and the '*' rule ----------------------------------------------------------------------------
def test_a_single_output_is_the_source_action(tmp_path, pkgroot, no_builtins, secrets):
    cfg = load(tmp_path, pkgroot, relay_body(secrets))
    assert cfg["sources"]["relay"]["action"] == "script:sink" and cfg["routes"] == []
    assert cfg["sources"]["relay"]["module"] == "_hookspool_bobbins.relay.source"
    errs = expand_errors(tmp_path, pkgroot, relay_body(secrets).replace('outputs = { "*" = "script:sink" }', ""))
    assert has(errs, "relay.outputs.*: required")


def test_several_outputs_become_routes_before_the_files_own_and_star_is_the_action(tmp_path, pkgroot,
                                                                                   no_builtins, secrets):
    body = f"""
[bobbins.ci]
bobbin = "fanout"
secrets.secret = {{ file = "{secrets['sig']}" }}
outputs = {{ build = "hermes:ci-build", "*" = "hermes:ci-other" }}

[[routes]]
name = "mine"
source = "ci"
match = {{ repo = "x" }}
destination = "hermes:mine"
"""
    cfg = load(tmp_path, pkgroot, body)
    assert cfg["sources"]["ci"]["action"] == "hermes:ci-other"
    assert cfg["sources"]["ci"]["module"] == PK.TEMPLATE_MODULE and cfg["sources"]["ci"]["sender_retries"] is True
    assert [r["name"] for r in cfg["routes"]] == ["ci-build", "mine"]
    assert cfg["routes"][0] == {"name": "ci-build", "source": "ci", "match": {"event_type": "build"},
                                "destination": "hermes:ci-build"}
    assert C.resolve_route(cfg, "ci", {"event_type": "build"}, None) == ("ci-build", "hermes:ci-build")
    assert C.resolve_route(cfg, "ci", {"event_type": "review"}, None) == ("ci", "hermes:ci-other")
    errs = expand_errors(tmp_path, pkgroot, body.replace(', "*" = "hermes:ci-other"', ""))
    assert has(errs, 'outputs."*": required for a package with several outputs')


# ---- collisions ------------------------------------------------------------------------------------------
def test_collisions_with_explicit_tables_are_errors(tmp_path, pkgroot, no_builtins, secrets):
    errs = expand_errors(tmp_path, pkgroot, digest_body(secrets) + '\n[sources.mail]\naction = "hermes:x"\n')
    assert has(errs, "sources.mail: also defined by [bobbins.mail]")
    errs = expand_errors(tmp_path, pkgroot, digest_body(secrets) + '\n[destinations."script:mail"]\nargv = ["/bin/true"]\n')
    assert has(errs, 'destinations."script:mail": also defined by [bobbins.mail]')
    body = f"""
[bobbins.ci]
bobbin = "fanout"
secrets.secret = {{ file = "{secrets['sig']}" }}
outputs = {{ build = "hermes:a", "*" = "hermes:b" }}
[[routes]]
name = "ci-build"
source = "ci"
destination = "hermes:c"
"""
    assert has(expand_errors(tmp_path, pkgroot, body), "routes.ci-build: also generated by [bobbins.ci]")


# ---- check rules on the expanded config ------------------------------------------------------------------
def write_subs(path, routes):
    path.write_text(json.dumps({r: {"secret": "s"} for r in routes}))


def checked(tmp_path, pkgroot, body, *, secrets_ok=True):
    subs = tmp_path / "subs.json"
    write_subs(subs, ["mail-summary", "mail-alert", "ci-build", "ci-other", "all", "mine"])
    cfg = load(tmp_path, pkgroot, body + f'\n[hermes]\nsubscriptions_path = "{subs}"\n')
    return C.check(cfg, check_secrets=secrets_ok)


def test_a_clean_expanded_config_passes_check(tmp_path, pkgroot, no_builtins, secrets):
    rep = checked(tmp_path, pkgroot, digest_body(secrets) + relay_body(secrets))
    assert rep.errors == [], rep.errors


def test_check_refuses_a_route_that_bypasses_the_processor(tmp_path, pkgroot, no_builtins, secrets):
    route = '\n[[routes]]\nname = "side"\nsource = "mail"\ndestination = "hermes:mail-alert"\n'
    rep = checked(tmp_path, pkgroot, digest_body(secrets) + route)
    assert has(rep.errors, "routes.side", "bypassing the package's processor")
    rep = checked(tmp_path, pkgroot, digest_body(secrets) + route + "tee = true\n")
    assert not has(rep.errors, "bypassing"), rep.errors
    rep = checked(tmp_path, pkgroot, digest_body(secrets) + route.replace("hermes:mail-alert", "script:mail"))
    assert not has(rep.errors, "bypassing"), rep.errors


def test_check_refuses_a_processor_output_to_a_missing_destination(tmp_path, pkgroot, no_builtins, secrets):
    body = digest_body(secrets).replace('alert = "hermes:mail-alert"', 'alert = "script:nowhere"')
    rep = checked(tmp_path, pkgroot, body)
    assert has(rep.errors, "bobbins.mail.outputs.alert", "script:nowhere")
    rep = checked(tmp_path, pkgroot, digest_body(secrets).replace('alert = "hermes:mail-alert"', 'alert = "quarantine"'))
    assert has(rep.errors, "bobbins.mail.outputs.alert", "reserved")


def test_check_refuses_env_references_for_processor_secrets(tmp_path, pkgroot, no_builtins, secrets, monkeypatch):
    monkeypatch.setenv("TEST_CRED", "x")
    body = digest_body(secrets).replace(f'secrets.cred = {{ file = "{secrets["cred"]}" }}',
                                        'secrets.cred = { env = "TEST_CRED" }')
    rep = checked(tmp_path, pkgroot, body)
    assert has(rep.errors, "bobbins.mail.secrets.cred", "does not inherit")
    body = digest_body(secrets).replace(f'secrets.token = {{ file = "{secrets["tok"]}" }}',
                                        'secrets.token = { env = "TEST_CRED" }')
    assert not has(checked(tmp_path, pkgroot, body).errors, "does not inherit")


def test_check_validates_every_discovered_manifest(tmp_path, pkgroot, no_builtins, secrets):
    make_pkg(pkgroot, "broken", "bogus = 1\n" + MINIMAL.format(name="broken"))
    os.chmod(pkgroot / "broken", 0o755)
    rep = checked(tmp_path, pkgroot, relay_body(secrets))
    assert has(rep.errors, "bobbin broken: bogus: unknown key")
    body = relay_body(secrets) + '\n[bobbins.b]\nbobbin = "broken"\nsecrets.secret = { file = "/x" }\n'
    assert has(expand_errors(tmp_path, pkgroot, body), "package 'broken' is invalid")


def test_user_config_cannot_set_processor_or_hookspool_env(tmp_path, pkgroot, no_builtins, secrets):
    body = relay_body(secrets) + """
[destinations."script:mine"]
processor = "relay"
[destinations."script:other"]
argv = ["/bin/true"]
env = { HOOKSPOOL_SETTINGS = "{}", lower = "x", GOOD = "ok", NUM = "1" }
[destinations."script:both"]
argv = ["/bin/true"]
env = { BAD = 1 }
[sources.s2]
module = "hookspool.sources.template"
scheme = "url-token"
key = "id"
action = "script:other"
secrets.secret = { file = "%s" }
""" % secrets["sig"]
    rep = checked(tmp_path, pkgroot, body)
    assert has(rep.errors, 'script:mine.processor: set only by a [bobbins.<instance>] expansion')
    assert has(rep.errors, "script:other.env.HOOKSPOOL_SETTINGS: HOOKSPOOL_* names are set by hookspool")
    assert has(rep.errors, "script:other.env.lower: names must match")
    assert has(rep.errors, "script:both.env.BAD: must be a string")
    assert not has(rep.errors, "env.GOOD")


# ---- code loaded by path: ownership (14.3.6) -------------------------------------------------------------
def test_group_writable_package_code_is_refused(tmp_path, pkgroot, no_builtins, secrets):
    os.chmod(pkgroot / "relay" / "source.py", 0o664)
    rep = checked(tmp_path, pkgroot, relay_body(secrets))
    assert has(rep.errors, "bobbins.relay: package code", "group- or other-writable")
    with pytest.raises(ImportError, match="group- or other-writable"):
        C.default_plugin_loader("relay", "_hookspool_bobbins.relay.source")
    os.chmod(pkgroot / "relay" / "source.py", 0o644)
    os.chmod(pkgroot, 0o777)
    rep = checked(tmp_path, pkgroot, relay_body(secrets))
    assert has(rep.errors, "spool.bobbin_path:", "group- or other-writable")


def test_package_code_owned_by_another_user_is_refused(tmp_path, pkgroot, no_builtins, secrets, monkeypatch):
    load(tmp_path, pkgroot, relay_body(secrets))
    real = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: real + 4242)
    if real == 0:
        pytest.skip("files owned by root are always allowed")
    rep = checked(tmp_path, pkgroot, relay_body(secrets))
    assert has(rep.errors, "not root or the user hookspool runs as")
    with pytest.raises(ImportError, match="not root or the user"):
        C.default_plugin_loader("relay", "_hookspool_bobbins.relay.source")


def test_a_hooks_module_must_pass_the_ownership_rules(tmp_path, pkgroot, no_builtins, secrets):
    hooks = tmp_path / "hooks.py"
    hooks.write_text("def classify(m, a):\n    return None\n")
    os.chmod(hooks, 0o644)
    body = digest_body(secrets).replace('settings = { owner = "someone@example.com" }',
                                        f'settings = {{ owner = "o", hooks = "{hooks}" }}')
    assert checked(tmp_path, pkgroot, body).errors == []
    os.chmod(hooks, 0o666)
    assert has(checked(tmp_path, pkgroot, body).errors, "settings.hooks", "group- or other-writable")
    missing = body.replace(str(hooks), str(tmp_path / "gone.py"))
    assert has(checked(tmp_path, pkgroot, missing).errors, "settings.hooks", "does not exist")


def test_path_package_source_loads_by_file_path_not_sys_path(tmp_path, pkgroot, no_builtins, secrets):
    load(tmp_path, pkgroot, relay_body(secrets))
    before = list(sys.path)
    m = C.default_plugin_loader("relay", "_hookspool_bobbins.relay.source")
    assert m.name == "relay" and m.__file__ == str(pkgroot / "relay" / "source.py")
    assert sys.path == before and str(pkgroot) not in sys.path
    assert C.default_plugin_loader("relay", "_hookspool_bobbins.relay.source") is m      # unchanged file: cached
    src = pkgroot / "relay" / "source.py"
    src.write_text(src.read_text().replace('name = "relay"', 'name = "relay2"'))
    os.utime(src, ns=(10**18, 10**18))
    assert C.default_plugin_loader("relay", "_hookspool_bobbins.relay.source").name == "relay2"
    with pytest.raises(ImportError, match="no bobbin"):
        C.default_plugin_loader("x", "_hookspool_bobbins.unknown.source")


# ---- service expand: golden output, no secrets ----------------------------------------------------------
GOLDEN = """# [bobbins.mail]: package digest ({root})
[sources.mail]
module = "_hookspool_bobbins.digest.source"
span = "mail"
expect_every_s = 86400
secrets = {{ token = {{ file = "{tok}" }}, cred = {{ file = "{cred}" }} }}
action = "script:mail"

[destinations."script:mail"]
processor = "mail"
max_inflight = 1
timeout_s = 120
state_dir = "{home}/services/mail"

[destinations."script:mail".env]
HOOKSPOOL_BOBBIN = "digest"
HOOKSPOOL_BOBBIN_INSTANCE = "mail"
HOOKSPOOL_BOBBIN_DIR = "{root}/digest"
HOOKSPOOL_SETTINGS = "{{\\"hooks\\":\\"\\",\\"labels\\":[\\"inbox\\"],\\"limit\\":25,\\"owner\\":\\"someone@example.com\\",\\"verbose\\":false}}"
HOOKSPOOL_DELTA_ROUTES = "{{\\"alert\\":\\"hermes:mail-alert\\",\\"summary\\":\\"hermes:mail-summary\\"}}"
HOOKSPOOL_SECRET_REFS = "<references of: cred>"

# [bobbins.ci]: package fanout ({root})
[sources.ci]
module = "hookspool.sources.template"
scheme = "hmac-sha256-hex"
header = "X-Signature"
prefix = "sha256="
key = "repo"
event_type = "kind"
attrs = {{ event_type = "kind" }}
sender_retries = true
span = "burst"
secrets = {{ secret = {{ file = "{sig}" }} }}
action = "hermes:ci-other"

[[routes]]
name = "ci-build"
source = "ci"
match = {{ event_type = "build" }}
destination = "hermes:ci-build"
"""

CI = """
[bobbins.ci]
bobbin = "fanout"
secrets.secret = {{ file = "{sig}" }}
outputs = {{ build = "hermes:ci-build", "*" = "hermes:ci-other" }}
"""


def run_cli(args, capsys):
    rc = main(args)
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_service_expand_golden_output_and_it_parses_back(tmp_path, pkgroot, no_builtins, secrets, capsys):
    p = tmp_path / "config.toml"
    p.write_text(cfg_text(tmp_path, pkgroot, digest_body(secrets) + CI.format(sig=secrets["sig"])))
    rc, out, err = run_cli(["--config", str(p), "bobbin", "expand"], capsys)
    assert rc == 0, err
    want = GOLDEN.format(root=pkgroot, home=tmp_path / "home", **secrets)
    assert out == want
    parsed = tomllib.loads(out)
    assert parsed["sources"]["mail"]["action"] == "script:mail"
    assert parsed["destinations"]["script:mail"]["env"]["HOOKSPOOL_SECRET_REFS"] == "<references of: cred>"
    rc, one, _ = run_cli(["--config", str(p), "bobbin", "expand", "ci"], capsys)
    assert rc == 0 and one.startswith("# [bobbins.ci]") and "[sources.mail]" not in one
    assert run_cli(["--config", str(p), "bobbin", "expand", "nope"], capsys)[0] == 1


def test_service_expand_never_prints_a_secret_value(tmp_path, pkgroot, no_builtins, secrets, capsys):
    envf = tmp_path / "cred.env"
    envf.write_text(f"CRED={SENTINEL}-envfile\n")
    os.chmod(envf, 0o600)
    body = digest_body(secrets).replace(f'secrets.cred = {{ file = "{secrets["cred"]}" }}',
                                        f'secrets.cred = {{ env_file = "{envf}", key = "CRED" }}')
    p = tmp_path / "config.toml"
    p.write_text(cfg_text(tmp_path, pkgroot, body + relay_body(secrets)))
    rc, out, err = run_cli(["--config", str(p), "bobbin", "expand"], capsys)
    assert rc == 0, err
    assert SENTINEL not in out and SENTINEL not in err
    assert secrets["tok"] in out and str(envf) not in out.split("HOOKSPOOL_SECRET_REFS")[1]
    assert "<references of: cred>" in out
    rc, out, err = run_cli(["--config", str(p), "--json", "bobbin", "show", "digest"], capsys)
    assert rc == 0 and SENTINEL not in out + err


def test_service_expand_reports_every_error(tmp_path, pkgroot, no_builtins, secrets, capsys):
    p = tmp_path / "config.toml"
    p.write_text(cfg_text(tmp_path, pkgroot, digest_body(secrets, "bogus = 1\n").replace(
        'settings = { owner = "someone@example.com" }', "")))
    rc, out, err = run_cli(["--config", str(p), "bobbin", "expand"], capsys)
    assert rc == 2 and out == ""
    assert "error: bobbins.mail.bogus: unknown key" in err and "error: bobbins.mail.settings.owner: required" in err
    rc, _, err = run_cli(["--config", str(p), "check"], capsys)
    assert rc == 2 and "error: bobbins.mail.bogus: unknown key" in err


# ---- bobbin list / show / new ---------------------------------------------------------------------------
def test_service_list_and_show(tmp_path, pkgroot, no_builtins, secrets, capsys):
    p = tmp_path / "config.toml"
    p.write_text(cfg_text(tmp_path, pkgroot, digest_body(secrets)))
    rc, out, _ = run_cli(["--config", str(p), "bobbin", "list"], capsys)
    assert rc == 0
    lines = out.splitlines()
    assert [ln.split()[0] for ln in lines] == ["digest", "fanout", "relay"]
    assert "[instances: mail]" in lines[0] and str(pkgroot) in lines[0]
    rc, out, _ = run_cli(["--config", str(p), "--json", "bobbin", "list"], capsys)
    rows = json.loads(out)
    assert rows[0]["name"] == "digest" and rows[0]["version"] == "3" and rows[0]["instances"] == ["mail"]
    rc, out, _ = run_cli(["--config", str(p), "bobbin", "show", "digest"], capsys)
    assert rc == 0
    for frag in ("digest 3", "span: mail", "sender_retries: True", "cred (processor)", "owner: string required",
                 "limit: int default=25", "hooks: path", "summary: a summary of new items", "state=True"):
        assert frag in out, (frag, out)
    rc, out, _ = run_cli(["--config", str(p), "bobbin", "show", "relay"], capsys)
    assert "proxy: nothing special" in out and "expect_every_s: 3600" in out
    assert run_cli(["--config", str(p), "bobbin", "show", "nope"], capsys)[0] == 1


@pytest.mark.parametrize("flags", [[], ["--template"], ["--processor"]])
def test_service_new_scaffolds_a_package_that_loads_and_checks(tmp_path, no_builtins, capsys, flags, secrets):
    root = tmp_path / "mine"
    root.mkdir(mode=0o755)
    os.chmod(tmp_path, 0o755)
    rc, out, err = run_cli(["bobbin", "new", "acme-hooks", "--dir", str(root), *flags], capsys)
    assert rc == 0, err
    d = root / "acme-hooks"
    assert (d / "bobbin.toml").is_file() and (d / "README.md").is_file() and (d / "captures" / "example.http").is_file()
    assert (d / "source.py").exists() is ("--template" not in flags)
    assert (d / "processor.py").exists() is ("--processor" in flags)
    pkg = PK.load_package(str(d), str(root), C.DEFAULTS["spans"])
    assert pkg.errors == []
    outputs = '{ changed = "hermes:acme" }' if "--processor" in flags else '{ "*" = "hermes:acme" }'
    body = f'[bobbins.acme]\nbobbin = "acme-hooks"\nsecrets.secret = {{ file = "{secrets["sig"]}" }}\noutputs = {outputs}\n'
    cfg = load(tmp_path, root, body)
    subs = tmp_path / "subs.json"
    write_subs(subs, ["acme"])
    cfg["hermes"]["subscriptions_path"] = str(subs)
    rep = C.check(cfg)
    assert rep.errors == [], rep.errors
    assert run_cli(["bobbin", "new", "acme-hooks", "--dir", str(root)], capsys)[0] == 1
    assert run_cli(["bobbin", "new", "Bad_Name", "--dir", str(root)], capsys)[0] == 2


def test_scaffolded_capture_verifies_against_the_scaffolded_source(tmp_path, no_builtins):
    from hookspool import plugin as P
    d = PK.scaffold("acme", str(tmp_path))
    os.chmod(tmp_path, 0o755)
    PK.register_path_module("_hookspool_bobbins.acme.source", os.path.join(d, "source.py"), d)
    src = P.load_source("acme", {"module": "_hookspool_bobbins.acme.source",
                                 "secrets": {"secret": {"file": "/unused"}}}, C.default_plugin_loader)
    with open(os.path.join(d, "captures", "example.http"), "rb") as f:
        raw = P.parse_capture(f.read())
    req = P.Request(raw.method, raw.target, raw.headers, raw.body, now=0.0)
    assert src.verify(req, {"secret": PK.SCAFFOLD_SECRET})
    assert not src.verify(req, {"secret": "wrong-secret"})
    assert src.key(req.json(), req) == "evt_1"


# ---- reload: intake and dispatcher see the expansion -----------------------------------------------------
def _bump(path, n):
    os.utime(path, ns=(10**18 + n * 10**9, 10**18 + n * 10**9))


def test_intake_reload_applies_an_expanded_config(tmp_path, pkgroot, no_builtins, secrets, make_store):
    from hookspool import intake as I
    from hookspool import verify as V
    p = tmp_path / "config.toml"
    p.write_text(cfg_text(tmp_path, pkgroot, ""))
    cfg = C.load_config(str(p))
    st = make_store(cfg)
    intake = I.Intake(cfg, st, config_path=str(p))
    _bump(p, 1)
    intake.maybe_reload(force=True)
    assert "relay" not in intake.cfg["sources"]
    p.write_text(cfg_text(tmp_path, pkgroot, relay_body(secrets)))
    _bump(p, 2)
    assert intake.maybe_reload(force=True) is True
    assert intake.cfg["sources"]["relay"]["action"] == "script:sink"
    body = json.dumps({"id": "k1", "type": "t", "note": SENTINEL}).encode()
    sig = "sha256=" + V.hmac_hex_sign(SENTINEL + "-sig", body)
    r = intake.handle("POST", "/hooks/relay", [("Content-Type", "application/json"), ("X-Signature", sig),
                                               ("Content-Length", str(len(body)))], io.BytesIO(body))
    assert r.status == 200, r.body
    assert json.loads(r.body)["status"] == "queued"
    rows = st.list(None, source="relay")
    assert rows and rows[0]["destination"] == "script:sink" and rows[0]["key"] == "k1"
    # an invalid package edit keeps the running config
    (pkgroot / "relay" / "bobbin.toml").write_text("name = \"relay\"\nbogus = 1\n")
    _bump(p, 3)
    assert intake.maybe_reload(force=True) is False
    assert "relay" in intake.cfg["sources"]


def test_dispatcher_reload_applies_an_expanded_config(rig, tmp_path, pkgroot, no_builtins, secrets):
    p = tmp_path / "config.toml"
    r = rig(config_path=str(p))
    subs = tmp_path / "subs.json"
    write_subs(subs, ["mail-summary", "mail-alert"])
    p.write_text(cfg_text(tmp_path, pkgroot, digest_body(secrets) + f'[hermes]\nsubscriptions_path = "{subs}"\n'))
    _bump(p, 1)
    r.d.tick()
    d = r.d.config["destinations"]["script:mail"]
    assert d["processor"] == "mail" and r.d.config["sources"]["mail"]["action"] == "script:mail"
    dest = ScriptDestination("script:mail", d, r.d.config)
    assert dest.command() == [sys.executable, "-I", "-m", "hookspool.processor", "mail"]
    # a broken instance keeps the previous config
    p.write_text(cfg_text(tmp_path, pkgroot, digest_body(secrets, "bogus = 1\n")))
    _bump(p, 2)
    r.d.tick()
    assert r.d.config["destinations"]["script:mail"]["processor"] == "mail"
    assert r.store.issue_get("config:file")["state"] == "open"


# ---- the script destination's env and processor ----------------------------------------------------------
def _ctx(timeout=30.0):
    import time
    now = time.time()
    return DeliveryContext(batch_id=7, attempt=1, attempt_id=1, delivery_id="dlv", probe=False,
                           lease_until=now + timeout + 5, deadline=now + timeout, now=time.time)


def test_script_env_reaches_the_child_over_env_file(tmp_path, monkeypatch):
    envf = tmp_path / "s.env"
    envf.write_text("FROM_FILE=file\nSHARED=file\n")
    out = tmp_path / "env.json"
    script = tmp_path / "dump.py"
    script.write_text("import json, os, sys\njson.dump(dict(os.environ), open(sys.argv[1], 'w'))\n")
    monkeypatch.setenv("DISPATCHER_ONLY", SENTINEL)
    spec = {"argv": [sys.executable, str(script), str(out)], "env_file": str(envf),
            "env": {"SHARED": "env", "FROM_TABLE": "table", "HOOKSPOOL_SETTINGS": '{"a":1}'}}
    d = ScriptDestination("script:dump", spec, C.merge(C.DEFAULTS, {}))
    o = d.deliver({}, b"{}", "dlv-1", _ctx())
    assert o.kind == "ok", o
    env = json.loads(out.read_text())
    assert env["FROM_FILE"] == "file" and env["SHARED"] == "env" and env["FROM_TABLE"] == "table"
    assert env["HOOKSPOOL_SETTINGS"] == '{"a":1}' and env["HOOKSPOOL_BATCH_ID"] == "7"
    assert env["HOOKSPOOL_DELIVERY_ID"] == "dlv-1" and "DISPATCHER_ONLY" not in env


def test_script_processor_builds_its_argv_at_spawn(tmp_path, monkeypatch):
    seen = {}

    class FakePopen:
        def __init__(self, argv, **kw):
            seen["argv"], seen["env"] = argv, kw["env"]
            raise OSError("not really")
    monkeypatch.setattr("hookspool.destinations.script.subprocess.Popen", FakePopen)
    d = ScriptDestination("script:mail", {"processor": "mail", "env": {"HOOKSPOOL_BOBBIN_INSTANCE": "mail"}},
                          C.merge(C.DEFAULTS, {}))
    o = d.deliver({}, b"{}", "dlv", _ctx())
    assert o.kind == "retry" and seen["argv"] == [sys.executable, "-I", "-m", "hookspool.processor", "mail"]
    assert seen["env"]["HOOKSPOOL_BOBBIN_INSTANCE"] == "mail"
    assert d.check() == []


# ---- round 1: reload keeps --home, more negative cases ---------------------------------------------------
def _homeless_config(tmp_path, pkgroot, body: str) -> str:
    """A config with no [spool] home, so --home is the only thing that places the processor's state."""
    home = tmp_path / "home"
    return cfg_text(tmp_path, pkgroot, body).replace(f'home = "{home}"\n', "")


def test_intake_reload_keeps_the_cli_home_and_so_the_processor_state_dir(tmp_path, pkgroot, no_builtins, secrets,
                                                                          monkeypatch, make_store):
    from hookspool import intake as I
    monkeypatch.delenv("HOOKSPOOL_HOME", raising=False)
    cli_home = str(tmp_path / "cli-home")
    subs = tmp_path / "subs.json"
    write_subs(subs, ["mail-summary", "mail-alert"])
    p = tmp_path / "config.toml"
    body = digest_body(secrets) + f'[hermes]\nsubscriptions_path = "{subs}"\n'
    p.write_text(_homeless_config(tmp_path, pkgroot, body))
    cfg = C.load_config(str(p), cli_home=cli_home)
    want = os.path.join(cli_home, "services", "mail")
    assert cfg["destinations"]["script:mail"]["state_dir"] == want
    intake = I.Intake(cfg, make_store(cfg), config_path=str(p), cli_home=cli_home)
    p.write_text(_homeless_config(tmp_path, pkgroot, body + "\n# edited\n"))
    _bump(p, 5)
    assert intake.maybe_reload(force=True) is True
    assert intake.cfg["destinations"]["script:mail"]["state_dir"] == want


def test_dispatcher_reload_keeps_the_cli_home_and_so_the_processor_state_dir(rig, tmp_path, pkgroot, no_builtins,
                                                                              secrets, monkeypatch):
    monkeypatch.delenv("HOOKSPOOL_HOME", raising=False)
    cli_home = str(tmp_path / "cli-home")
    subs = tmp_path / "subs.json"
    write_subs(subs, ["mail-summary", "mail-alert"])
    p = tmp_path / "config.toml"
    body = digest_body(secrets) + f'[hermes]\nsubscriptions_path = "{subs}"\n'
    r = rig(config_path=str(p), cli_home=cli_home)
    p.write_text(_homeless_config(tmp_path, pkgroot, body))
    _bump(p, 1)
    r.d.tick()
    want = os.path.join(cli_home, "services", "mail")
    assert r.d.config["destinations"]["script:mail"]["state_dir"] == want
    p.write_text(_homeless_config(tmp_path, pkgroot, body + "\n# edited\n"))
    _bump(p, 2)
    r.d.tick()
    assert r.d.config["destinations"]["script:mail"]["state_dir"] == want


def test_check_refuses_argv_and_processor_on_an_expanded_table(tmp_path, pkgroot, no_builtins, secrets):
    cfg = load(tmp_path, pkgroot, digest_body(secrets))
    cfg["destinations"]["script:mail"]["argv"] = ["/bin/true"]
    rep = C.check(cfg, check_secrets=False)
    assert has(rep.errors, "script:mail", "argv and processor are exclusive"), rep.errors


@pytest.mark.parametrize("secrets_line,outputs_line,want", [
    ("secrets = 5", 'outputs = { "*" = "script:sink" }', "bobbins.relay.secrets: must be a table"),
    ("secrets = []", 'outputs = { "*" = "script:sink" }', "bobbins.relay.secrets: must be a table"),
    ("secrets.secret = { file = \"{sig}\" }", 'outputs = "script:sink"', "bobbins.relay.outputs: must be a table"),
    ("secrets.secret = { file = \"{sig}\" }", 'outputs = ["script:sink"]', "bobbins.relay.outputs: must be a table"),
])
def test_secrets_and_outputs_must_be_tables(tmp_path, pkgroot, no_builtins, secrets, secrets_line, outputs_line, want):
    text = (f"\n[bobbins.relay]\n{secrets_line.replace('{sig}', secrets['sig'])}\n{outputs_line}\n"
            '[destinations."script:sink"]\nargv = ["/bin/true"]\n')
    assert has(expand_errors(tmp_path, pkgroot, text), want)


def test_service_list_marks_an_invalid_package_and_warns(tmp_path, pkgroot, no_builtins, secrets, capsys):
    make_pkg(pkgroot, "broken", "bogus = 1\n" + MINIMAL.format(name="broken"))
    p = tmp_path / "config.toml"
    p.write_text(cfg_text(tmp_path, pkgroot, ""))
    rc, out, err = run_cli(["--config", str(p), "bobbin", "list"], capsys)
    assert rc == 0
    line = next(ln for ln in out.splitlines() if ln.startswith("broken"))
    assert line.endswith("(invalid)")
    assert not any(ln.endswith("(invalid)") for ln in out.splitlines() if not ln.startswith("broken"))
    assert "warning: bobbin broken: bogus: unknown key" in err
    rc, out, _ = run_cli(["--config", str(p), "--json", "bobbin", "list"], capsys)
    assert {r["name"]: r["valid"] for r in json.loads(out)}["broken"] is False


MULTI_CODE = MINIMAL.format(name="multi").replace('template = { scheme = "url-token", key = "id" }',
                                                   'module = "source"').replace(
    '[outputs."*"]\ndescription = "all"', '[outputs.a]\n[outputs.b]\n[outputs."*"]\ndescription = "all"')
SOURCE_PY = (
    "from hookspool.plugin import Request, Secrets\n"
    "name = 'multi'\nsecrets = ('secret',)\n\n\n"
    "def verify(req: Request, secrets: Secrets) -> bool:\n    return True\n\n\n"
    "def key(payload, req):\n    return str(payload['id'])\n\n\n"
    "def event_type(payload, req):\n    return str(payload.get('type'))\n")
MULTI_BODY = """
[bobbins.multi]
secrets.secret = {{ file = "{sig}" }}
outputs = {{ a = "script:sink", "*" = "script:sink" }}
[destinations."script:sink"]
argv = ["/bin/true"]
"""


def test_check_warns_when_a_multi_output_code_source_has_no_attrs(tmp_path, pkgroot, no_builtins, secrets):
    assert manifest_errors(MULTI_CODE, "multi") == []
    make_pkg(pkgroot, "multi", MULTI_CODE, {"source.py": SOURCE_PY})
    body = MULTI_BODY.format(sig=secrets["sig"])
    rep = C.check(load(tmp_path, pkgroot, body), check_secrets=False)
    assert rep.errors == [], rep.errors
    assert has(rep.warnings, "bobbins.multi", "does not expose event_type in attrs"), rep.warnings
    # with attrs exposing event_type there is no warning
    (pkgroot / "multi" / "source.py").write_text(
        SOURCE_PY + "\n\ndef attrs(payload, req):\n    return {'event_type': str(payload.get('type'))}\n")
    rep = C.check(load(tmp_path, pkgroot, body), check_secrets=False)
    assert not has(rep.warnings, "event_type in attrs"), rep.warnings
    # a single-output instance routes nothing by event_type, so it never warns
    rep = C.check(load(tmp_path, pkgroot, body.replace('a = "script:sink", ', "")), check_secrets=False)
    assert not has(rep.warnings, "event_type in attrs"), rep.warnings


def test_check_warns_when_a_multi_output_template_has_no_event_type_attr(tmp_path, pkgroot, no_builtins, secrets):
    body = f"""
[bobbins.ci]
bobbin = "fanout"
secrets.secret = {{ file = "{secrets['sig']}" }}
outputs = {{ build = "hermes:ci-build", "*" = "hermes:ci-other" }}
"""
    assert not has(C.check(load(tmp_path, pkgroot, body), check_secrets=False).warnings, "event_type in attrs")
    toml = (pkgroot / "fanout" / "bobbin.toml")
    toml.write_text(toml.read_text().replace(', attrs = { event_type = "kind" }', ""))
    rep = C.check(load(tmp_path, pkgroot, body), check_secrets=False)
    assert has(rep.warnings, "bobbins.ci", "does not expose event_type in attrs"), rep.warnings
