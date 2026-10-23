"""The ``webhook`` bobbin (ARCHITECTURE 14.3.2a) and the template-settings rule it rests on: an instance's
settings that name template keys are the template source's keys, whether the manifest's template is fixed
or ``"settings"``; every scheme expands and accepts its capture; a bad setting fails ``check`` with the
template's own message; ``hookspool bobbin new`` freezes the same settings into a fixed template and the
result passes the bobbin kit except for its TODO placeholders."""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tomllib

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.dirname(HERE)
sys.path.insert(0, TESTS)
sys.path.insert(0, os.path.dirname(TESTS))

from hookspool import bobbin_loader as PK  # noqa: E402
from hookspool import config as C  # noqa: E402
from hookspool import intake as I  # noqa: E402
from hookspool.__main__ import main  # noqa: E402
from hookspool.plugin import parse_capture  # noqa: E402

WEBHOOK = os.path.join(os.path.dirname(os.path.dirname(HERE)), "hookspool", "bobbins", "webhook")
SCHEMES_DIR = os.path.join(WEBHOOK, "captures", "schemes")
SECRET = "test-secret-bobbin"
CALLBACK = "https://hooks.example.test/hooks/webhook"

# the settings each shipped scheme capture is read with
SCHEME_SETTINGS = {
    "hmac-sha256-hex": {"scheme": "hmac-sha256-hex", "header": "X-Signature", "prefix": "sha256=", "key": ["id"]},
    "hmac-sha256-base64": {"scheme": "hmac-sha256-base64", "header": "X-Signature", "key": ["id"]},
    "hmac-sha1-base64-url": {"scheme": "hmac-sha1-base64-url", "header": "X-Signature", "callback_url": CALLBACK,
                             "key": ["id"]},
    "shared-secret-header": {"scheme": "shared-secret-header", "header": "X-Webhook-Token", "key": ["id"]},
    "url-token": {"scheme": "url-token", "key": ["id"]},
}


def secret_file(tmp_path, value: str = SECRET) -> str:
    p = tmp_path / "secret"
    p.write_text(value)
    os.chmod(p, 0o600)
    return str(p)


def instance(tmp_path, settings: dict, *, outputs=None, secret: str = SECRET) -> dict:
    return {"bobbin": "webhook", "settings": settings, "secrets": {"secret": {"file": secret_file(tmp_path, secret)}},
            "outputs": outputs or {"*": "hermes:webhook"}}


def expand(instances: dict) -> dict:
    return PK.expand(C.merge(C.DEFAULTS, {"bobbins": instances, "alerts": {"argv": ["true"]}}))


def capture(scheme: str):
    with open(os.path.join(SCHEMES_DIR, scheme + ".http"), "rb") as f:
        return parse_capture(f.read())


def expand_errors(instances: dict) -> list[str]:
    with pytest.raises(PK.BobbinError) as ei:
        expand(instances)
    return ei.value.errors


def check_errors(tmp_path, inst: dict, name: str = "webhook") -> list[str]:
    """What ``hookspool check`` reports for this one instance through the real config path."""
    subs = tmp_path / "subs.json"
    subs.write_text(json.dumps({name: {"secret": "s"}}))
    body = f"[bobbins.{name}]\n" + "\n".join(f"{k} = {PK._val(v)}" for k, v in inst.items()) + "\n"
    p = tmp_path / "config.toml"
    p.write_text(f'[spool]\nhome = "{tmp_path / "home"}"\n[alerts]\ndry = true\n[hermes]\nsubscriptions_path = "{subs}"\n'
                 + body)
    return C.check(C.load_config(str(p))).errors


# ---- the manifest and the kit ------------------------------------------------------------------------------
def test_the_manifest_is_a_settings_template_with_one_setting_per_template_key():
    pkgs, errs = PK.discover(C.merge(C.DEFAULTS, {}))
    assert errs == []
    pkg = pkgs["webhook"]
    assert pkg.errors == [] and pkg.builtin and pkg.source["template"] == "settings"
    assert set(pkg.settings) == set(PK.TEMPLATE_KEYS)
    assert set(pkg.secrets) == {"secret"} and set(pkg.outputs) == {"*"} and pkg.processor is None
    assert {n for n, s in pkg.settings.items() if s.get("required")} == {"scheme", "key"}
    for n, s in pkg.settings.items():
        assert s["type"] in {t for t in PK.TEMPLATE_KEYS[n]}, n


def test_every_scheme_has_a_capture_and_a_setting_set():
    shipped = sorted(f[:-5] for f in os.listdir(SCHEMES_DIR) if f.endswith(".http"))
    assert shipped == sorted(SCHEME_SETTINGS)
    from hookspool.sources import template as T
    assert set(T.SCHEMES) == set(shipped)


# ---- an instance per scheme expands and accepts its capture -------------------------------------------------
@pytest.mark.parametrize("scheme", sorted(SCHEME_SETTINGS))
def test_an_instance_per_scheme_expands_and_accepts_its_capture(scheme, tmp_path):
    out = expand({"webhook": instance(tmp_path, SCHEME_SETTINGS[scheme])})
    src = out["sources"]["webhook"]
    assert src["module"] == PK.TEMPLATE_MODULE and src["action"] == "hermes:webhook"
    for k, v in SCHEME_SETTINGS[scheme].items():                      # the settings are the template keys
        assert src[k] == v
    assert not {"param", "prefix", "callback_url", "dedup", "ignore", "attrs", "event_type"} & (
        set(src) - set(SCHEME_SETTINGS[scheme]))                      # an unset (empty) setting is not copied
    assert src["head_handshake"] is False and src["sender_retries"] is False     # a bool is always concrete
    raw = capture(scheme)
    assert I.source_test(out, "webhook", raw)["result"] == "queued"


@pytest.mark.parametrize("scheme", sorted(SCHEME_SETTINGS))
def test_a_wrong_secret_refuses_the_capture(scheme, tmp_path):
    out = expand({"webhook": instance(tmp_path, SCHEME_SETTINGS[scheme], secret="test-secret-other")})
    got = I.source_test(out, "webhook", capture(scheme))
    assert got["result"] == "unverified" and got["status"] == 401


@pytest.mark.parametrize("scheme", sorted(SCHEME_SETTINGS))
def test_each_scheme_instance_checks_clean(scheme, tmp_path):
    inst = instance(tmp_path, SCHEME_SETTINGS[scheme])
    errs = check_errors(tmp_path, inst)
    assert errs == [], errs


def test_optional_settings_reach_the_source_when_given_and_not_when_empty(tmp_path):
    settings = dict(SCHEME_SETTINGS["hmac-sha256-hex"], dedup="header:X-Delivery-Id", ignore=["type=ping"],
                    event_type="type", attrs={"kind": "type", "who": "header:X-User"}, head_handshake=True,
                    sender_retries=True)
    src = expand({"webhook": instance(tmp_path, settings)})["sources"]["webhook"]
    for k in ("dedup", "ignore", "event_type", "attrs", "head_handshake", "sender_retries"):
        assert src[k] == settings[k], k
    bare = expand({"webhook": instance(tmp_path, SCHEME_SETTINGS["hmac-sha256-hex"])})["sources"]["webhook"]
    assert set(bare) - {"module", "action", "secrets", "span"} == set(SCHEME_SETTINGS["hmac-sha256-hex"]) | {
        "head_handshake", "sender_retries"}
    assert bare["span"] == "burst"


def test_a_key_may_be_given_as_a_list_of_selectors_and_the_dedup_and_ignore_rules_work(tmp_path):
    settings = dict(SCHEME_SETTINGS["hmac-sha256-hex"], key=["literal:acme", "id"], dedup="body_sha256",
                    ignore=["type=order.created"])
    out = expand({"webhook": instance(tmp_path, settings)})
    assert out["sources"]["webhook"]["key"] == ["literal:acme", "id"]
    assert I.source_test(out, "webhook", capture("hmac-sha256-hex"))["result"] == "ignored"


# ---- a bad setting fails check with the template's own message ------------------------------------------------
@pytest.mark.parametrize("settings,message", [
    ({"scheme": "hmac-sha256-hex", "key": ["id"]}, "header: required by scheme hmac-sha256-hex"),
    ({"scheme": "hmac-sha1-base64-url", "header": "X-Sig", "key": ["id"]}, "callback_url: required by scheme"),
    ({"scheme": "url-token", "header": "X-Sig", "key": ["id"]}, "header: not used by scheme url-token"),
    ({"scheme": "nope", "header": "X-Sig", "key": ["id"]}, "scheme: required, one of hmac-sha256-hex"),
    ({"scheme": "hmac-sha256-hex", "header": "Bad Header", "key": ["id"]}, "header: not a valid header name"),
    ({"scheme": "hmac-sha256-hex", "header": "X-Sig", "key": []}, "key: required: a selector or a list of selectors"),
    ({"scheme": "hmac-sha256-hex", "header": "X-Sig", "key": ["a b"]}, "not a dotted path"),
    ({"scheme": "hmac-sha256-hex", "header": "X-Sig", "key": ["id"], "ignore": ["no-equals"]},
     "expected \"<selector>=<value>\""),
    ({"scheme": "hmac-sha256-hex", "header": "X-Sig", "key": ["id"], "attrs": {"a b": "id"}}, "not a valid attribute name"),
    ({"scheme": "url-token", "key": ["id"], "param": "a b"}, "param: must be a query parameter name"),
    ({"scheme": "hmac-sha256-hex", "header": "X-Sig", "key": ["id"], "dedup": "literal:x"},
     "a literal selector is not allowed here"),
])
def test_a_bad_setting_fails_check_with_the_templates_own_message(settings, message, tmp_path):
    errs = check_errors(tmp_path, instance(tmp_path, settings))
    assert any(message in e and "webhook" in e for e in errs), errs


@pytest.mark.parametrize("settings,message", [
    ({"key": ["id"]}, "settings.scheme: required by bobbin 'webhook'"),
    ({"scheme": "url-token"}, "settings.key: required by bobbin 'webhook'"),
    ({"scheme": "url-token", "key": "id"}, "settings.key: must be a list"),
    ({"scheme": "url-token", "key": ["id"], "attrs": "x"}, "settings.attrs: must be a table"),
    ({"scheme": "url-token", "key": ["id"], "head_handshake": "yes"}, "settings.head_handshake: must be true or false"),
    ({"scheme": "url-token", "key": ["id"], "nope": 1}, "settings.nope: bobbin 'webhook' has no such setting"),
])
def test_a_setting_of_the_wrong_type_or_missing_is_an_expansion_error(settings, message, tmp_path):
    errs = expand_errors({"webhook": instance(tmp_path, settings)})
    assert any(message in e for e in errs), errs


def test_a_missing_secret_is_an_expansion_error(tmp_path):
    inst = instance(tmp_path, SCHEME_SETTINGS["url-token"])
    del inst["secrets"]
    assert any("secrets.secret: required by bobbin 'webhook'" in e for e in expand_errors({"webhook": inst}))


def test_several_instances_are_configuration_not_bobbins(tmp_path):
    a = instance(tmp_path, SCHEME_SETTINGS["url-token"], outputs={"*": "hermes:a"})
    b = instance(tmp_path, SCHEME_SETTINGS["hmac-sha256-hex"], outputs={"*": "hermes:b"})
    out = expand({"a": a, "b": b})
    assert out["sources"]["a"]["scheme"] == "url-token" and out["sources"]["b"]["scheme"] == "hmac-sha256-hex"
    assert "header" not in out["sources"]["a"]


def test_an_output_per_event_type_becomes_a_route_on_event_type(tmp_path):
    out = expand({"webhook": instance(tmp_path, SCHEME_SETTINGS["url-token"],
                                      outputs={"*": "hermes:all", "order.created": "hermes:orders"})})
    assert out["routes"] == [{"name": "webhook-order.created", "source": "webhook",
                              "match": {"event_type": "order.created"}, "destination": "hermes:orders"}]


# ---- one rule for both template forms ----------------------------------------------------------------------
def fixture_manifest(source: str, settings: str = "", extra: str = "") -> str:
    return ('name = "pkg"\nversion = "1"\n\n' + source + '\n[secrets.secret]\ndescription = "x"\n\n' + settings
            + '\n[outputs."*"]\ndescription = "all"\n' + extra)


def manifest_errors(text: str) -> list[str]:
    return PK.validate_manifest(tomllib.loads(text), "pkg", C.DEFAULTS["spans"])


FIXED = '[source]\ntemplate = { scheme = "hmac-sha256-hex", header = "X-Sig" }\n'
SETTINGS_TPL = '[source]\ntemplate = "settings"\n'


@pytest.mark.parametrize("source", [FIXED, SETTINGS_TPL], ids=["fixed", "settings"])
def test_both_forms_apply_the_same_rule_to_a_setting_named_after_a_template_key(source):
    ok = fixture_manifest(source, '[settings.key]\ntype = "list"\nrequired = true\n')
    assert manifest_errors(ok) == []
    s = fixture_manifest(source, '[settings.key]\ntype = "string"\nrequired = true\n')
    assert manifest_errors(s) == []                                    # a key may be one selector or a list
    wrong = fixture_manifest(source, '[settings.key]\ntype = "int"\nrequired = true\n')
    assert any("settings.key.type" in e and "maps to the template key key" in e for e in manifest_errors(wrong))
    nokey = fixture_manifest(source, '[settings.who]\ntype = "string"\nrequired = true\n')
    assert any("settings.who" in e and "maps to no template key" in e and "dropped" in e
               for e in manifest_errors(nokey))
    with_proc = nokey + '\n[processor]\nmodule = "processor"\n'         # a processor reads what the template does not
    assert manifest_errors(with_proc) == []


def test_a_fixed_template_refuses_a_setting_for_a_key_it_already_sets():
    m = fixture_manifest(FIXED, '[settings.header]\ntype = "string"\nrequired = true\n')
    assert any("settings.header" in e and "already sets header" in e for e in manifest_errors(m))
    m = fixture_manifest(SETTINGS_TPL, '[settings.header]\ntype = "string"\nrequired = true\n')
    assert manifest_errors(m) == []


def test_source_sender_retries_and_a_setting_of_that_name_are_not_both_allowed():
    m = fixture_manifest(SETTINGS_TPL + 'sender_retries = true\n', '[settings.sender_retries]\ntype = "bool"\ndefault = false\n')
    assert any("[source] already sets sender_retries" in e for e in manifest_errors(m))


def test_the_template_value_is_a_table_or_settings():
    for bad in ('template = "other"', "template = 3"):
        m = fixture_manifest("[source]\n" + bad + "\n")
        assert any("source.template" in e and "'settings'" in e for e in manifest_errors(m)), bad


def test_a_fixed_template_bobbin_maps_settings_the_same_way(tmp_path):
    """The same expansion code serves a fixed template (trello's callback_url) and `template = "settings"`."""
    root = tmp_path / "pkgs"
    d = root / "pkg"
    d.mkdir(parents=True)
    (d / "bobbin.toml").write_text(fixture_manifest(
        '[source]\ntemplate = { scheme = "hmac-sha1-base64-url", header = "X-Sig" }\n',
        '[settings.callback_url]\ntype = "string"\nrequired = true\n'
        '[settings.key]\ntype = "list"\nrequired = true\n[settings.dedup]\ntype = "string"\ndefault = ""\n'))
    for p in (root, d):
        os.chmod(p, 0o755)
    os.chmod(tmp_path, 0o755)
    cfg = C.merge(C.DEFAULTS, {"spool": {"bobbin_path": [str(root)], "home": str(tmp_path / "h")},
                               "alerts": {"argv": ["true"]},
                               "bobbins": {"pkg": {"settings": {"callback_url": CALLBACK, "key": ["id"]},
                                                   "secrets": {"secret": {"file": secret_file(tmp_path)}},
                                                   "outputs": {"*": "hermes:pkg"}}}})
    src = PK.expand(cfg)["sources"]["pkg"]
    assert (src["scheme"], src["header"], src["callback_url"], src["key"]) == (
        "hmac-sha1-base64-url", "X-Sig", CALLBACK, ["id"])
    assert "dedup" not in src


# ---- bobbin new freezes the settings --------------------------------------------------------------------------
def run(args, capsys):
    rc = main(args)
    out = capsys.readouterr()
    return rc, out.out, out.err


def scaffolded(tmp_path, capsys, *flags):
    root = tmp_path / "mine"
    root.mkdir(mode=0o755, exist_ok=True)
    os.chmod(tmp_path, 0o755)
    rc, out, err = run(["bobbin", "new", "acme-hooks", "--dir", str(root), *flags], capsys)
    assert rc == 0, err
    return root, root / "acme-hooks"


@pytest.mark.parametrize("scheme", sorted(SCHEME_SETTINGS))
def test_bobbin_new_freezes_the_webhook_settings_into_a_fixed_template(scheme, tmp_path, capsys):
    flags = ["--set", f"scheme={scheme}"]
    if scheme == "url-token":
        flags += ["--set", "header="]
    _root, d = scaffolded(tmp_path, capsys, *flags)
    m = tomllib.loads((d / "bobbin.toml").read_text())
    tpl = m["source"]["template"]
    assert isinstance(tpl, dict) and tpl["scheme"] == scheme and "settings" not in m
    assert set(tpl) <= set(PK.TEMPLATE_KEYS) - {"sender_retries"}
    assert set(m["secrets"]) == {"secret"} and set(m["outputs"]) == {"*"}
    assert m["source"]["span"] == tomllib.loads(open(os.path.join(WEBHOOK, "bobbin.toml")).read())["source"]["span"]
    assert not (d / "source.py").exists()


@pytest.mark.parametrize("scheme", sorted(SCHEME_SETTINGS))
def test_the_frozen_bobbin_expands_to_the_source_the_webhook_instance_gives(scheme, tmp_path, capsys):
    flags = ["--set", f"scheme={scheme}"]
    if scheme == "url-token":
        flags += ["--set", "header="]
    root, d = scaffolded(tmp_path, capsys, *flags)
    tpl = tomllib.loads((d / "bobbin.toml").read_text())["source"]["template"]
    sec = secret_file(tmp_path)
    spec = {"secrets": {"secret": {"file": sec}}, "outputs": {"*": "hermes:acme"}}
    frozen = PK.expand(C.merge(C.DEFAULTS, {"spool": {"bobbin_path": [str(root)], "home": str(tmp_path / "h")},
                                            "alerts": {"argv": ["true"]},
                                            "bobbins": {"acme": dict(spec, bobbin="acme-hooks")}}))["sources"]["acme"]
    # the webhook declares `key` and `ignore` as lists; a fixed template may say one selector as a string
    as_lists = {k: ([v] if k in ("key", "ignore") and isinstance(v, str) else v) for k, v in copy.deepcopy(tpl).items()}
    via_webhook = expand({"acme": dict(spec, bobbin="webhook", settings=as_lists)})["sources"]["acme"]
    for k in ("key", "ignore"):
        if isinstance(frozen.get(k), str):
            frozen[k] = [frozen[k]]
    for d in (frozen, via_webhook):                                    # a bool the webhook always states
        d.pop("sender_retries", None)
        d.pop("head_handshake", None)
    assert frozen == via_webhook


@pytest.mark.parametrize("scheme", sorted(SCHEME_SETTINGS))
def test_the_scaffold_capture_is_accepted_and_a_wrong_secret_refused(scheme, tmp_path, capsys):
    flags = ["--set", f"scheme={scheme}"]
    if scheme == "url-token":
        flags += ["--set", "header="]
    root, d = scaffolded(tmp_path, capsys, *flags)

    def result(secret: str, name: str) -> str:
        cfg = PK.expand(C.merge(C.DEFAULTS, {
            "spool": {"bobbin_path": [str(root)], "home": str(tmp_path / "h")}, "alerts": {"argv": ["true"]},
            "bobbins": {"acme": {"bobbin": "acme-hooks", "secrets": {"secret": {"file": secret_file(tmp_path, secret)}},
                                 "outputs": {"*": "hermes:acme"}}}}))
        with open(d / "captures" / name, "rb") as f:
            raw = parse_capture(f.read())
        return I.source_test(cfg, "acme", raw)["result"]
    assert result(PK.SCAFFOLD_SECRET, "example.http") == "queued"
    assert result(PK.SCAFFOLD_SECRET, "bad_signature.http") == "unverified"
    assert result("test-secret-other", "example.http") == "unverified"


@pytest.mark.parametrize("flags,message", [
    (["--set", "scheme=hmac-sha256-hex", "--set", "header="], "header: required by scheme hmac-sha256-hex"),
    (["--set", "scheme=nope"], "scheme: required, one of"),
    (["--set", "nope=1"], "nope: not a template key"),
    (["--set", "head_handshake=\"x\""], "head_handshake: must be true or false"),
    (["--set", "novalue"], "expected NAME=VALUE"),
    (["--set", "key=3"], "key: must be a string or a list"),
    (["--code", "--set", "scheme=url-token"], "a code bobbin has none"),
])
def test_bobbin_new_refuses_a_bad_setting_with_the_templates_message(flags, message, tmp_path, capsys):
    rc, _out, err = run(["bobbin", "new", "acme-hooks", "--dir", str(tmp_path), *flags], capsys)
    assert rc == 2 and message in err, err
    assert not (tmp_path / "acme-hooks").exists()


def test_the_default_scaffold_is_the_webhook_copy_and_code_is_the_old_shape(tmp_path, capsys):
    _root, d = scaffolded(tmp_path, capsys)
    assert not (d / "source.py").exists() and isinstance(tomllib.loads((d / "bobbin.toml").read_text())["source"]["template"], dict)
    shutil.rmtree(d)
    _root, d = scaffolded(tmp_path, capsys, "--code")
    assert (d / "source.py").is_file() and tomllib.loads((d / "bobbin.toml").read_text())["source"]["module"] == "source"


def test_the_old_template_flag_is_still_accepted(tmp_path, capsys):
    _root, d = scaffolded(tmp_path, capsys, "--template")
    assert not (d / "source.py").exists()
