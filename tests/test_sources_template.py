"""The ``template`` source plugin (contract C4, PLAN "Decision: extension model").

* validation of the source table (every rule, through ``bind`` and through ``config.check``)
* each verification scheme against signatures computed here with ``hmac``/``hashlib`` directly
* selector semantics (key, dedup, event_type, attrs, ignore), handshake, quarantine on an unreadable key
* the shared conformance kit (``tests/sourcekit.py``) over EVERY template bobbin (``hookspool/bobbins/
  <vendor>/<product>/bobbin.toml`` with ``[source.template]``), the captures in the bobbin's ``captures/``
  through ``hookspool source test``, and the CLI; test ids carry the bobbin's name (``[unsigned-github.repo]``)

Adding a template source is: a bobbin (``hookspool/bobbins/<vendor>/<product>/bobbin.toml`` with ``[source.template]``
and ``secrets.secret``) plus ``captures/event.http`` (a request signed with ``TEMPLATE_SECRET``, expect
queued); optional ``*.http`` captures for ping, tampered, HEAD. Regenerate signed captures with
``python tests/test_sources_template.py --write <name>``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import tomllib

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sourcekit  # noqa: E402
from sourcekit import SENTINEL, Env, Kit  # noqa: E402

from hookspool import config as hs_config  # noqa: E402
from hookspool import plugin as P  # noqa: E402
from hookspool.__main__ import main  # noqa: E402
from hookspool.plugin import BadPayload, Request, Response, parse_capture  # noqa: E402
from hookspool.sources import template as T  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = [sourcekit.bobbin_manifest(n) for n in ("github", "kaneo", "trello")]
TEMPLATE_SECRET = "test-template-secret-" + SENTINEL
MODULE = "hookspool.sources.template"


# ---- independent signers (hmac/hashlib only; nothing from hookspool.verify) -------------------------
def _sign(cfg: dict, raw: P.RawRequest, secret: str) -> P.RawRequest:
    scheme, key = cfg["scheme"], secret.encode()
    if scheme == "hmac-sha256-hex":
        return raw.set(cfg["header"], cfg.get("prefix", "") + hmac.new(key, raw.body, hashlib.sha256).hexdigest())
    if scheme == "hmac-sha256-base64":
        mac = base64.b64encode(hmac.new(key, raw.body, hashlib.sha256).digest()).decode()
        return raw.set(cfg["header"], cfg.get("prefix", "") + mac)
    if scheme == "hmac-sha1-base64-url":
        msg = raw.body + cfg["callback_url"].encode()
        return raw.set(cfg["header"], base64.b64encode(hmac.new(key, msg, hashlib.sha1).digest()).decode())
    if scheme == "shared-secret-header":
        return raw.set(cfg["header"], cfg.get("prefix", "") + secret)
    if scheme == "url-token":
        sep = "&" if "?" in raw.target else "?"
        return P.RawRequest(raw.method, f"{raw.target}{sep}{cfg.get('param', 'token')}={secret}",
                            list(raw.headers), raw.body, dict(raw.meta))
    raise AssertionError(scheme)


def _unsign(cfg: dict, raw: P.RawRequest) -> P.RawRequest:
    if cfg["scheme"] == "url-token":
        return P.RawRequest(raw.method, raw.target.split("?")[0], list(raw.headers), raw.body, dict(raw.meta))
    return raw.set(cfg["header"], None)


def _req(raw: P.RawRequest, now: float = 0.0) -> Request:
    return Request(raw.method, raw.target, raw.headers, raw.body, now=now)


def _post(body: bytes | dict, headers: dict | None = None, target: str = "/hooks/t") -> P.RawRequest:
    if isinstance(body, dict):
        body = json.dumps(body, separators=(",", ":")).encode()
    return P.RawRequest("POST", target, list((headers or {}).items()), body)


# ---- examples -> kits -------------------------------------------------------------------------------------
def _name(path: str) -> str:
    """The bobbin's name, ``<vendor>.<product>``, from its manifest's path."""
    d = os.path.dirname(path)
    return f"{os.path.basename(os.path.dirname(d))}.{os.path.basename(d)}"


def load_example(path: str) -> tuple[str, dict]:
    """(the sample instance's source name, its expanded [sources.<name>] table)."""
    name = _name(path)
    assert path == sourcekit.bobbin_manifest(name), f"{path}: expected the bobbin manifest"
    with open(path, "rb") as f:
        assert "secret" in tomllib.load(f).get("secrets", {}), f"{path}: declares secrets.secret"
    cfg = sourcekit.bobbin_source_cfg(name)
    assert cfg.get("module") == MODULE, f"{path}: module must be {MODULE}"
    return sourcekit.SOURCE_OF[name], cfg


def example_kit(path: str) -> Kit:
    name, cfg = load_example(path)
    event = os.path.join(sourcekit.capture_dir(name), "event.http")
    assert os.path.exists(event), f"tests/captures/{name}/event.http is required"
    with open(event, "rb") as f:
        sample = _unsign(cfg, parse_capture(f.read()))
    sample.meta.clear()
    assert SENTINEL.encode() in sample.body, "the event capture must carry the sentinel in its body"
    head = os.path.join(sourcekit.capture_dir(name), "head.http")
    unauth = None
    if cfg.get("head_handshake"):
        with open(head, "rb") as f:
            unauth = parse_capture(f.read())
        unauth.meta.clear()
    src_cfg = {k: v for k, v in cfg.items() if k != "secrets"}
    return Kit(name=name, module=T.bind(name, cfg), secrets={"secret": TEMPLATE_SECRET}, sample=sample,
               sign=lambda raw, secrets, now, _c=cfg: _sign(_c, raw, secrets["secret"]), source_cfg=src_cfg,
               unauth_handshake=unauth)


def test_there_are_examples():
    assert {_name(p) for p in EXAMPLES} == {"github.repo", "kaneo.projects", "atlassian.trello"}


@pytest.mark.parametrize("path", EXAMPLES, ids=[_name(p) for p in EXAMPLES])
@pytest.mark.parametrize("case", sourcekit.CASES, ids=sourcekit.CASE_IDS)
def test_conformance_kit_on_example(case, path, tmp_path, clock):
    sourcekit.run(case, example_kit(path), tmp_path, clock)


CAPTURE_FILES = [(_name(p), c) for p in EXAMPLES
                 for c in sourcekit.captures(_name(p))]


@pytest.mark.parametrize("name,cap", CAPTURE_FILES, ids=[f"{n}/{os.path.basename(c)}" for n, c in CAPTURE_FILES])
def test_captures(name, cap, tmp_path, clock):
    kit = example_kit(sourcekit.bobbin_manifest(name))
    out = sourcekit.run_capture(cap, kit, tmp_path, clock)
    assert out["source"] == sourcekit.SOURCE_OF[name]


@pytest.mark.parametrize("path", EXAMPLES, ids=[_name(p) for p in EXAMPLES])
def test_example_captures_cover_the_outcomes(path):
    name, cfg = load_example(path)
    results = {}
    for c in sourcekit.captures(name):
        with open(c, "rb") as f:
            results[os.path.basename(c)] = parse_capture(f.read()).meta["expect"]
    assert results.get("event.http") == "queued", results
    assert "unverified" in results.values(), "a tampered capture is required"
    if cfg.get("ignore"):
        assert "ignored" in results.values()
    if cfg.get("head_handshake"):
        assert "challenge" in results.values()


def _cli_config(tmp_path, name: str) -> str:
    text = sourcekit.source_table_text(name)
    sec = tmp_path / "secret"
    sec.write_text(TEMPLATE_SECRET)
    os.chmod(sec, 0o600)
    p = tmp_path / "hookspool.toml"
    p.write_text(f'[spool]\nhome = "{tmp_path / "home"}"\n\n[alerts]\ndry = true\n\n{text}\n'
                 f'secrets.secret = {{ file = "{sec}" }}\n')
    return str(p)


@pytest.mark.parametrize("name,cap", CAPTURE_FILES, ids=[f"{n}/{os.path.basename(c)}" for n, c in CAPTURE_FILES])
def test_source_test_cli_with_the_default_loader(name, cap, tmp_path, capsys):
    """`hookspool source test` imports the plugin by module path and binds it to the table."""
    with open(cap, "rb") as f:
        raw = parse_capture(f.read())
    rc = main(["--config", _cli_config(tmp_path, name), "source", "test", sourcekit.SOURCE_OF[name], "--request", cap])
    out = capsys.readouterr().out
    doc = json.loads(out)
    assert doc["result"] == raw.meta["expect"]
    assert rc == (0 if doc["result"] in ("queued", "ignored", "challenge") else 1)
    assert SENTINEL not in out


@pytest.mark.parametrize("path", EXAMPLES, ids=[_name(p) for p in EXAMPLES])
def test_config_check_accepts_the_example(path, tmp_path):
    name, cfg = load_example(path)
    full = hs_config.merge(hs_config.DEFAULTS, {"sources": {name: cfg}, "alerts": {"argv": ["true"]}})
    rep = hs_config.check(full, check_secrets=False)
    assert [e for e in rep.errors if e.startswith(f"sources.{name}")] == [], rep.errors


def test_config_check_uses_the_template_keys_and_secret_requirement(tmp_path):
    base = {"module": MODULE, "scheme": "hmac-sha256-hex", "header": "X-S", "key": "id", "action": "hermes:t"}
    cfg = hs_config.merge(hs_config.DEFAULTS, {"sources": {"t": dict(base, bogus=1)},
                                               "alerts": {"argv": ["true"]}})
    rep = hs_config.check(cfg, check_secrets=False)
    assert any("sources.t" in e and "bogus" in e for e in rep.errors), rep.errors
    assert any("secrets.secret" in e and "required" in e for e in rep.errors), rep.errors
    assert not any("scheme" in e for e in rep.errors)


def test_config_check_reports_every_template_error_without_values(tmp_path):
    cfg = hs_config.merge(hs_config.DEFAULTS, {
        "sources": {"t": {"module": MODULE, "scheme": "nope", "key": 7, "action": "hermes:t",
                          "secrets": {"secret": {"env": "NO_SUCH_HOOKSPOOL_VAR_" + SENTINEL}}}},
        "alerts": {"argv": ["true"]}})
    rep = hs_config.check(cfg, check_secrets=False)
    msgs = [e for e in rep.errors if e.startswith("sources.t: ")]
    assert any("scheme" in m for m in msgs) and any("key" in m for m in msgs), msgs
    assert not any("bogus" in m or "unknown key" in m for m in msgs), "template keys are known even when binding fails"


def test_config_check_warns_about_url_tokens():
    cfg = hs_config.merge(hs_config.DEFAULTS, {
        "sources": {"t": {"module": MODULE, "scheme": "url-token", "key": "id", "action": "hermes:t",
                          "secrets": {"secret": {"env": "X"}}}},
        "alerts": {"argv": ["true"]}})
    rep = hs_config.check(cfg, check_secrets=False)
    assert any("url-token" in w and w.startswith("sources.t") for w in rep.warnings), rep.warnings


def test_keep_headers_cannot_carry_the_signature_header():
    cfg = hs_config.merge(hs_config.DEFAULTS, {
        "sources": {"t": {"module": MODULE, "scheme": "hmac-sha256-hex", "header": "X-Sig", "key": "id",
                          "action": "hermes:t", "keep_headers": ["x-sig"], "secrets": {"secret": {"env": "X"}}}},
        "alerts": {"argv": ["true"]}})
    rep = hs_config.check(cfg, check_secrets=False)
    assert any("x-sig" in e and "auth header" in e for e in rep.errors), rep.errors


# ---- validation -------------------------------------------------------------------------------------------
GOOD = {"scheme": "hmac-sha256-hex", "header": "X-Sig", "key": "id"}


def _errors(**over) -> list[str]:
    cfg = dict(GOOD)
    for k, v in over.items():
        if v is None:
            cfg.pop(k, None)
        else:
            cfg[k] = v
    with pytest.raises(P.PluginError) as e:
        T.bind("t", cfg)
    assert isinstance(e.value, T.TemplateConfigError)
    return e.value.errors


@pytest.mark.parametrize("over,needle", [
    ({"scheme": None}, "scheme: required"),
    ({"scheme": "md5"}, "scheme: required, one of"),
    ({"header": None}, "header: required by scheme"),
    ({"header": "bad header"}, "header: not a valid header name"),
    ({"header": ""}, "header: not a valid header name"),
    ({"prefix": ""}, "prefix: must be"),
    ({"prefix": 3}, "prefix: must be"),
    ({"param": "t"}, "param: not used by scheme"),
    ({"callback_url": "https://x.test/"}, "callback_url: not used by scheme"),
    ({"key": None}, "key: required"),
    ({"key": []}, "key: required"),
    ({"key": 5}, "key: required"),
    ({"key": "a b"}, "key: 'a b': not a dotted path"),
    ({"key": "a..b"}, "key: 'a..b': not a dotted path"),
    ({"key": "header:bad name"}, "key: 'header:bad name': not a header name"),
    ({"key": "literal:"}, "key: 'literal:': empty constant"),
    ({"key": "body_sha256"}, "key: 'body_sha256': a body sha256 selector is not allowed here"),
    ({"dedup": "literal:x"}, "dedup: 'literal:x': a literal selector is not allowed here"),
    ({"dedup": ""}, "dedup: must be a non-empty string"),
    ({"event_type": "body_sha256"}, "event_type: 'body_sha256'"),
    ({"attrs": "x"}, "attrs: must be a table"),
    ({"attrs": {"bad name": "id"}}, "attrs: 'bad name': not a valid attribute name"),
    ({"attrs": {"ok": "a b"}}, "attrs.ok: 'a b'"),
    ({"ignore": "noequals"}, "ignore: 'noequals': expected"),
    ({"ignore": "a="}, "ignore: 'a=': expected"),
    ({"ignore": 5}, "ignore: must be"),
    ({"ignore": "literal:x=y"}, "ignore: 'literal:x': a literal selector is not allowed here"),
    ({"head_handshake": "yes"}, "head_handshake: must be true or false"),
    ({"sender_retries": 1}, "sender_retries: must be true or false"),
])
def test_validation_messages(over, needle):
    errs = _errors(**over)
    assert any(needle in e for e in errs), errs


def test_validation_reports_every_problem_at_once():
    with pytest.raises(T.TemplateConfigError) as e:
        T.bind("t", {"scheme": "hmac-sha1-base64-url", "key": "a b", "ignore": "x", "head_handshake": 3})
    fields = {m.split(":")[0] for m in e.value.errors}
    assert {"header", "callback_url", "key", "ignore", "head_handshake"} <= fields


@pytest.mark.parametrize("scheme,extra", [
    ("hmac-sha256-hex", {"header": "X-S"}), ("hmac-sha256-base64", {"header": "X-S", "prefix": "sha256="}),
    ("hmac-sha1-base64-url", {"header": "X-S", "callback_url": "https://h.test/hooks/t"}),
    ("shared-secret-header", {"header": "X-S"}), ("url-token", {})])
def test_every_scheme_binds_and_declares_its_auth_headers(scheme, extra):
    m = T.bind("t", {"scheme": scheme, "key": "id", **extra})
    assert m.secrets == ("secret",)
    assert m.auth_headers == ((extra["header"],) if "header" in extra else ())
    assert callable(m.verify) and callable(m.key)
    assert not hasattr(m, "challenge") and not hasattr(m, "ignore") and not hasattr(m, "dedup_id")


def test_the_loaded_source_wraps_the_bound_module():
    cfg = {"module": MODULE, "scheme": "shared-secret-header", "header": "X-Tok", "key": "id",
           "action": "hermes:t", "keep_headers": ["X-Tok", "X-Keep"], "head_handshake": True,
           "secrets": {"secret": {"env": "X"}}}
    src = P.load_source("t", cfg)
    assert src.auth_headers == ("X-Tok",) and src.challenge_unauthenticated and src.has_challenge
    assert src.keep_headers == ("X-Keep",), "an auth header is never kept"
    assert src.secret_names == ("secret",)
    with pytest.raises(T.TemplateConfigError):
        P.load_source("t", {"module": MODULE, "scheme": "nope", "key": "id"})


def test_two_sources_on_one_module_do_not_share_state():
    a = T.bind("a", {"scheme": "hmac-sha256-hex", "header": "X-A", "key": "a.id"})
    b = T.bind("b", {"scheme": "shared-secret-header", "header": "X-B", "key": "b.id"})
    assert a.auth_headers == ("X-A",) and b.auth_headers == ("X-B",)
    assert a.key({"a": {"id": 1}, "b": {"id": 2}}, Request("POST", "/")) == "1"
    assert b.key({"a": {"id": 1}, "b": {"id": 2}}, Request("POST", "/")) == "2"


def test_module_level_verify_uses_the_request_config():
    cfg = {"scheme": "shared-secret-header", "header": "X-Tok", "key": "id"}
    ok = Request("POST", "/", {"X-Tok": "s3cret"}, b"{}", config=cfg)
    assert T.verify(ok, {"secret": "s3cret"}) is True
    assert T.verify(Request("POST", "/", {"X-Tok": "other"}, b"{}", config=cfg), {"secret": "s3cret"}) is False
    assert T.verify(Request("POST", "/", {}, b"{}", config={"scheme": "nope"}), {"secret": "s"}) is False


# ---- schemes ----------------------------------------------------------------------------------------------
SCHEME_CFGS = {
    "hmac-sha256-hex": {"scheme": "hmac-sha256-hex", "header": "X-Sig"},
    "hmac-sha256-hex+prefix": {"scheme": "hmac-sha256-hex", "header": "X-Sig", "prefix": "sha256="},
    "hmac-sha256-base64": {"scheme": "hmac-sha256-base64", "header": "X-Sig"},
    "hmac-sha256-base64+prefix": {"scheme": "hmac-sha256-base64", "header": "X-Sig", "prefix": "v1="},
    "hmac-sha1-base64-url": {"scheme": "hmac-sha1-base64-url", "header": "X-Sig",
                             "callback_url": "https://hooks.example.test/hooks/t"},
    "shared-secret-header": {"scheme": "shared-secret-header", "header": "X-Sig"},
    "shared-secret-header+prefix": {"scheme": "shared-secret-header", "header": "X-Sig", "prefix": "Token "},
    "url-token": {"scheme": "url-token"},
    "url-token+param": {"scheme": "url-token", "param": "auth"},
}


@pytest.fixture(params=sorted(SCHEME_CFGS))
def scheme_case(request):
    cfg = dict(SCHEME_CFGS[request.param], key="id")
    return cfg, T.bind("t", cfg)


def test_scheme_accepts_a_valid_signature_only(scheme_case):
    cfg, m = scheme_case
    raw = _post({"id": "1", "note": "hello"})
    secrets = {"secret": "right-secret"}
    assert m.verify(_req(_sign(cfg, raw, "right-secret")), secrets) is True
    assert m.verify(_req(raw), secrets) is False, "unsigned"
    assert m.verify(_req(_sign(cfg, raw, "wrong-secret")), secrets) is False, "wrong secret"
    signed = _sign(cfg, raw, "right-secret")
    if cfg["scheme"] != "shared-secret-header" and cfg["scheme"] != "url-token":
        assert m.verify(_req(signed.with_body(raw.body + b" ")), secrets) is False, "tampered body"
    assert m.verify(_req(signed), {"secret": ""}) is False, "an empty secret never verifies"


def test_scheme_rejects_garbage_headers(scheme_case):
    cfg, m = scheme_case
    if cfg["scheme"] == "url-token":
        for target in ("/hooks/t?token=", "/hooks/t?x=right-secret", "/hooks/t?auth=", "/hooks/t"):
            assert m.verify(Request("POST", target, [], b"{}"), {"secret": "right-secret"}) is False
        return
    for junk in ("", " ", "!!!!", "0" * 5000, "sha256=", "v1=", "Token "):
        assert m.verify(_req(_post(b"{}", {"X-Sig": junk})), {"secret": "right-secret"}) is False, junk


def test_url_token_is_not_read_from_a_header_and_the_param_name_matters():
    m = T.bind("t", {"scheme": "url-token", "param": "auth", "key": "id"})
    sec = {"secret": "tok"}
    assert m.verify(Request("POST", "/h?auth=tok", [], b"{}"), sec) is True
    assert m.verify(Request("POST", "/h?token=tok", [], b"{}"), sec) is False
    assert m.verify(Request("POST", "/h", [("auth", "tok")], b"{}"), sec) is False


def test_hex_signature_is_case_insensitive_and_sha1_secret_is_not_the_url():
    m = T.bind("t", {"scheme": "hmac-sha256-hex", "header": "X-Sig", "key": "id"})
    body = b'{"id":"1"}'
    mac = hmac.new(b"s", body, hashlib.sha256).hexdigest().upper()
    assert m.verify(_req(_post(body, {"X-Sig": mac})), {"secret": "s"}) is True
    t = T.bind("t", {"scheme": "hmac-sha1-base64-url", "header": "X-Sig", "key": "id",
                     "callback_url": "https://a.test/x"})
    mac = base64.b64encode(hmac.new(b"s", body + b"https://a.test/y", hashlib.sha1).digest()).decode()
    assert t.verify(_req(_post(body, {"X-Sig": mac})), {"secret": "s"}) is False, "a different callback URL"


def test_the_template_never_compares_or_hashes_by_itself():
    src = open(T.__file__).read()
    for banned in ("compare_digest", "hmac.new", "import hmac", "hashlib.", "import hashlib"):
        assert banned not in src


# ---- selectors and stages -----------------------------------------------------------------------------------
def _mod(**over):
    cfg = {"scheme": "shared-secret-header", "header": "X-Tok", "key": "a.b"}
    cfg.update(over)
    return T.bind("t", cfg)


def test_key_paths_headers_literals_and_lists():
    payload = {"a": {"b": "x", "c": 7, "d": True, "e": 1.5}, "l": [{"n": "zero"}, {"n": "one"}], "e": ""}
    req = Request("POST", "/", {"X-Id": "h1"}, b"")
    assert _mod(key="a.b").key(payload, req) == "x"
    assert _mod(key="a.c").key(payload, req) == "7"
    assert _mod(key="a.d").key(payload, req) == "true"
    assert _mod(key="a.e").key(payload, req) == "1.5"
    assert _mod(key="l.1.n").key(payload, req) == "one"
    assert _mod(key="header:x-id").key(payload, req) == "h1", "headers are case-insensitive"
    assert _mod(key="literal:all").key(payload, req) == "all"
    assert _mod(key=["a.b", "header:X-Id", "literal:z"]).key(payload, req) == "x:h1:z"


@pytest.mark.parametrize("key", ["a.missing", "a", "l", "e", "header:X-Absent", "l.9.n", "a.b.c"])
def test_an_unreadable_key_raises_bad_payload(key):
    payload = {"a": {"b": "x"}, "l": [1], "e": ""}
    with pytest.raises(BadPayload) as e:
        _mod(key=key).key(payload, Request("POST", "/"))
    assert key in str(e.value) and "x" not in str(e.value).replace(key, "")


def test_a_list_valued_key_part_is_an_error_not_a_repr():
    with pytest.raises(BadPayload):
        _mod(key=["a.b", "l"]).key({"a": {"b": "x"}, "l": [1]}, Request("POST", "/"))


def test_dedup_event_type_attrs():
    payload = {"id": 42, "ev": "push", "r": {"n": "o/r"}}
    req = Request("POST", "/", {"X-D": "d1"}, b'{"id":42}')
    m = _mod(dedup="id", event_type="ev", attrs={"repo": "r.n", "hd": "header:X-D", "gone": "r.nothing"})
    assert m.dedup_id(payload, req) == "42" and m.event_type(payload, req) == "push"
    assert m.attrs(payload, req) == {"repo": "o/r", "hd": "d1"}, "a missing attr is omitted"
    assert _mod(dedup="header:X-D").dedup_id(payload, req) == "d1"
    assert _mod(dedup="header:X-Nope").dedup_id(payload, req) is None
    assert _mod(dedup="r.nothing").dedup_id(payload, req) is None
    assert _mod(dedup="body_sha256").dedup_id(payload, req) == hashlib.sha256(req.body).hexdigest()
    assert _mod(event_type="r.nothing").event_type(payload, req) == ""


def test_ignore_rules_header_path_list_and_non_json():
    m = _mod(ignore=["header:X-Event=ping", "action=deleted", "n.0=skip"])
    ping = Request("POST", "/", {"X-Event": "ping"}, b"not json")
    assert m.ignore(ping) == "header:X-Event=ping", "a header rule never parses the body"
    r = Request("POST", "/", {"X-Event": "push"}, json.dumps({"action": "deleted"}).encode())
    assert m.ignore(r) == "action=deleted"
    assert m.ignore(Request("POST", "/", {"X-Event": "push"}, b'{"action":"created","n":["keep"]}')) is None
    assert m.ignore(Request("POST", "/", {}, b'{"n":["skip"]}')) == "n.0=skip"
    assert m.ignore(Request("POST", "/", {}, b'{"action":true}')) is None
    assert _mod(ignore="flag=true").ignore(Request("POST", "/", {}, b'{"flag":true}')) == "flag=true"
    with pytest.raises(BadPayload):
        m.ignore(Request("POST", "/", {"X-Event": "push"}, b"not json"))


def test_head_handshake_is_constant_and_only_for_head():
    m = _mod(head_handshake=True)
    assert m.challenge_unauthenticated is True
    a = m.challenge(Request("HEAD", "/hooks/t", {"User-Agent": "a"}), {})
    b = m.challenge(Request("HEAD", "/hooks/t?x=1", {"User-Agent": "b", "X": "y"}), {})
    assert isinstance(a, Response) and a.key() == b.key() and a.status == 200 and a.body == b""
    assert m.challenge(Request("POST", "/hooks/t", {}, b"{}"), {}) is None
    assert not hasattr(_mod(), "challenge_unauthenticated")


def test_sender_retries_and_expect_every_s_flow_to_the_source():
    m = T.bind("t", {"scheme": "url-token", "key": "id", "sender_retries": False, "expect_every_s": 3600})
    src = P.Source("t", m, {"action": "hermes:t"})
    assert src.sender_retries is False and src.expect_every_s == 3600
    assert P.Source("t", _mod(), {}).sender_retries is None


# ---- through the intake ------------------------------------------------------------------------------------
def _env(tmp_path, clock, **cfg):
    full = {"scheme": "hmac-sha256-hex", "header": "X-Sig", "key": "id", "action": "hermes:t"}
    full.update(cfg)
    if full.get("header") is ...:
        del full["header"]
    sample = _post({"id": "k1", "note": SENTINEL}, {"Content-Type": "application/json"}, "/hooks/t")
    kit = Kit(name="t", module=T.bind("t", full), secrets={"secret": TEMPLATE_SECRET}, sample=sample,
              sign=lambda raw, secrets, now: _sign(full, raw, secrets["secret"]), source_cfg=full)
    return Env(kit, tmp_path, clock)


def test_an_unreadable_key_is_quarantined_not_5xxd(tmp_path, clock):
    e = _env(tmp_path, clock, key="no.such.path")
    try:
        r = e.send(e.signed())
        assert r.status == 202 and json.loads(r.body)["status"] == "quarantined"
        b = e.batches()
        assert len(b) == 1 and b[0]["state"] == "dead" and "BadPayload in key" in b[0]["last_error"]
        assert SENTINEL not in b[0]["last_error"]
        e.assert_clean_logs()
    finally:
        e.close()


def test_a_non_json_body_with_a_valid_signature_is_quarantined(tmp_path, clock):
    e = _env(tmp_path, clock)
    try:
        raw = _sign(e.kit.source_cfg, _post(b"plain text " + SENTINEL.encode(), target="/hooks/t"), TEMPLATE_SECRET)
        r = e.send(raw)
        assert r.status == 202, (r.status, r.body)
        assert e.batches()[0]["destination"] == "quarantine"
    finally:
        e.close()


def test_route_hint_and_attrs_reach_route_matching(tmp_path, clock):
    e = _env(tmp_path, clock, attrs={"kind": "kind"}, ignore="kind=skip")
    try:
        ok = _post({"id": "k1", "kind": "ci", "note": SENTINEL}, target="/hooks/t")
        assert json.loads(e.send(e.signed(ok)).body)["status"] == "queued"
        skipped = _post({"id": "k2", "kind": "skip", "note": SENTINEL}, target="/hooks/t")
        r = e.send(e.signed(skipped))
        assert r.status == 200 and json.loads(r.body)["status"] == "ignored"
        assert len(e.events()) == 1
    finally:
        e.close()


def test_url_token_source_end_to_end_and_the_query_is_not_stored(tmp_path, clock):
    e = _env(tmp_path, clock, scheme="url-token", header=...)
    try:
        r = e.send(e.signed())
        assert r.status == 200 and json.loads(r.body)["status"] == "queued"
        assert TEMPLATE_SECRET.encode() not in json.dumps(
            [dict(x) for x in e.events()], default=lambda b: bytes(b).decode("latin-1")).encode()
        assert e.send(e.kit.sample).status == 401
        e.assert_no_secret_in_db()
    finally:
        e.close()


# ---- capture generation (maintenance) -----------------------------------------------------------------------
def write_captures(name: str) -> None:
    """Re-sign tests/captures/<name>/*.http with TEMPLATE_SECRET (run after editing a body). A
    capture with ``# tamper_of: <file>`` carries that file's signature over a changed body."""
    _, cfg = load_example(sourcekit.bobbin_manifest(name))
    d = sourcekit.capture_dir(name)
    for path in sourcekit.captures(name):
        with open(path, "rb") as f:
            raw = parse_capture(f.read())
        if raw.meta.get("expect") == "challenge":
            continue
        basis = raw
        if raw.meta.get("tamper_of"):
            with open(os.path.join(d, raw.meta["tamper_of"]), "rb") as f:
                basis = parse_capture(f.read())
        signed = _sign(cfg, _unsign(cfg, basis), TEMPLATE_SECRET)
        signed = P.RawRequest(signed.method, signed.target, signed.headers, raw.body, dict(raw.meta))
        with open(path, "wb") as f:
            f.write(P.format_capture(signed))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--write":
        write_captures(sys.argv[2])
    else:
        print(__doc__)
