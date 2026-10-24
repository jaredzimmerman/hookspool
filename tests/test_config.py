"""Contract C5: TOML config, presets, per-field inheritance, secret refs and every `check` rule."""
import json
import os
import types

import pytest

from conftest import SENTINEL, write_subs
from hookspool import config as C


def plugin(**kw):
    base = dict(auth_headers=("X-Hub-Signature-256",), keep_headers=("X-GitHub-Event",), secrets=("secret",),
                span="burst", action="hermes:github", sender_retries=True, config_keys=())
    base.update(kw)
    return types.SimpleNamespace(**base)


def loader(plugins):
    def load(name, module):
        if name not in plugins:
            raise ImportError(name)
        return plugins[name]
    return load


@pytest.fixture
def secret_file(tmp_path):
    p = tmp_path / "gh.secret"
    p.write_text(SENTINEL + "\n")
    os.chmod(p, 0o600)
    return str(p)


@pytest.fixture
def base(tmp_path, secret_file, subs_path):
    """A clean config: github through two routes, a script destination, an http destination."""
    write_subs(subs_path, {"github-ci": {"secret": "s", "events": []}, "github": {"secret": "s"},
                           "github-prs": {"secret": "s"}})
    sec = tmp_path / "audit.secret"
    sec.write_text("whsec_dGVzdA==")
    os.chmod(sec, 0o600)
    text = f"""
[spool]
home = "{tmp_path / 'home'}"

[hermes]
subscriptions_path = "{subs_path}"

[sources.github]
span = "burst"
secrets.secret = {{ file = "{secret_file}" }}
max_body_bytes = 786432

[sources.gcal]
span = "calendar"
secrets.secret = {{ file = "{secret_file}" }}
action = "script:gcal_delta"
expect_every_s = 172800

[[routes]]
name = "github-ci"
source = "github"
match = {{ class = "ci" }}
destination = "hermes:github-ci"

[[routes]]
name = "github-prs"
source = "github"
match = {{ class = "pr" }}
destination = "hermes:github-prs"
quiet_s = 60

[destinations."script:gcal_delta"]
argv = ["/usr/bin/python3", "/opt/scripts/gcal_delta.py", "--batch"]
cwd = "/opt/scripts"
timeout_s = 300
state_dir = "/var/lib/hookspool/scripts/gcal"

[destinations.audit]
type = "http"
url = "https://example.invalid/hooks/audit"
secret = {{ file = "{sec}" }}
headers = {{ "X-Env" = "test" }}
"""
    p = tmp_path / "config.toml"
    p.write_text(text)
    return str(p)


PLUGINS = {"github": plugin(), "gcal": plugin(auth_headers=("X-Goog-Channel-Token",), keep_headers=(),
                                              span="calendar", action="script:gcal_delta")}


def run_check(cfg, plugins=PLUGINS, **kw):
    return C.check(cfg, load_plugin=loader(plugins), **kw)


def has(msgs, *parts):
    return any(all(p in m for p in parts) for m in msgs)


# ---- loading and paths --------------------------------------------------------------------------
def test_missing_file_means_defaults(tmp_path):
    cfg = C.load_config(str(tmp_path / "nope.toml"))
    assert cfg["_path"] is None and cfg["coalesce"] == {"quiet_s": 0, "max_s": 0, "max_events": 500,
                                                        "max_bytes": 786432}
    assert C.check(cfg).ok


def test_invalid_toml_and_non_utf8_raise(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[spool\nhome = 1")
    with pytest.raises(C.ConfigError, match="not valid TOML"):
        C.load_config(str(p))
    p.write_bytes(b"\xff\xfe")
    with pytest.raises(C.ConfigError):
        C.load_config(str(p))


def test_json_config_is_not_accepted(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(json.dumps({"spool": {"path": "/x"}}))
    with pytest.raises(C.ConfigError):
        C.load_config(str(p))


def test_merge_over_defaults(base):
    cfg = C.load_config(base)
    assert cfg["_path"] == base
    assert cfg["dispatch"]["breaker"] == {"trip_after": 3, "cooldown_initial_s": 30, "cooldown_max_s": 600}
    assert cfg["hermes"]["base_url"] == "http://127.0.0.1:8644"
    assert [r["name"] for r in cfg["routes"]] == ["github-ci", "github-prs"]


def test_path_precedence(tmp_path, monkeypatch):
    for k in ("HOOKSPOOL_HOME", "HOOKSPOOL_CONFIG", "HOOKSPOOL_DB"):
        monkeypatch.delenv(k, raising=False)
    cfg = C.merge(C.DEFAULTS, {})
    assert C.home_dir(cfg) == "/var/lib/hookspool"
    assert C.config_path() == "/var/lib/hookspool/config.toml"
    assert C.db_path(cfg) == "/var/lib/hookspool/spool.db"
    cfg = C.merge(C.DEFAULTS, {"spool": {"home": str(tmp_path / "cfghome"), "db": "s.db"}})
    assert C.db_path(cfg) == str(tmp_path / "cfghome" / "s.db")
    monkeypatch.setenv("HOOKSPOOL_HOME", str(tmp_path / "envhome"))
    assert C.home_dir(cfg) == str(tmp_path / "envhome")                    # env beats config
    assert C.config_path() == str(tmp_path / "envhome" / "config.toml")
    assert C.home_dir(cfg, str(tmp_path / "cli")) == str(tmp_path / "cli")  # flag beats env
    monkeypatch.setenv("HOOKSPOOL_CONFIG", str(tmp_path / "x.toml"))
    assert C.config_path() == str(tmp_path / "x.toml")
    assert C.config_path(str(tmp_path / "flag.toml")) == str(tmp_path / "flag.toml")
    monkeypatch.setenv("HOOKSPOOL_DB", str(tmp_path / "env.db"))
    assert C.db_path(cfg) == str(tmp_path / "env.db")
    assert C.db_path(cfg, str(tmp_path / "flag.db")) == str(tmp_path / "flag.db")


def test_env_switches(monkeypatch):
    cfg = C.merge(C.DEFAULTS, {})
    monkeypatch.delenv("HOOKSPOOL_ALERT_DRY", raising=False)
    assert not C.alerts_dry(cfg) and C.alerts_dry(C.merge(cfg, {"alerts": {"dry": True}}))
    monkeypatch.setenv("HOOKSPOOL_ALERT_DRY", "1")
    assert C.alerts_dry(cfg)
    monkeypatch.setenv("HOOKSPOOL_LOG", "json")
    assert C.log_format() == "json"
    monkeypatch.setenv("HOOKSPOOL_LOG", "text")
    assert C.log_format() == "text"


# ---- presets and windows --------------------------------------------------------------------------
def test_span_presets():
    assert {k: (v["quiet_s"], v["max_s"]) for k, v in C.DEFAULTS["spans"].items()} == {
        "burst": (120, 600), "mail": (30, 120), "calendar": (45, 180), "sync": (60, 300), "meeting": (60, 900),
        "immediate": (0, 0)}


def test_window_inherited_per_field(base):
    cfg = C.load_config(base)
    cfg["sources"]["github"]["max_events"] = 50
    cfg["coalesce"]["max_bytes"] = 500000
    assert C.window_for(cfg, "github", "github-prs") == {"quiet_s": 60.0, "max_s": 600.0, "max_events": 50,
                                                         "max_bytes": 500000}
    assert C.window_for(cfg, "github", "github-ci") == {"quiet_s": 120.0, "max_s": 600.0, "max_events": 50,
                                                        "max_bytes": 500000}
    assert C.window_for(cfg, "gcal") == {"quiet_s": 45.0, "max_s": 180.0, "max_events": 500, "max_bytes": 500000}
    assert C.window_for(cfg, "unknown") == {"quiet_s": 0.0, "max_s": 0.0, "max_events": 500, "max_bytes": 500000}
    assert C.window_for(cfg, "unknown", plugin_span="sync")["quiet_s"] == 60.0
    cfg["spans"]["custom"] = {"quiet_s": 7, "max_s": 8}
    cfg["sources"]["gcal"]["span"] = "custom"
    assert (C.window_for(cfg, "gcal")["quiet_s"], C.window_for(cfg, "gcal", plugin_span="sync")["max_s"]) == (7, 8)


def test_source_body_cap_clamped_to_route(base):
    cfg = C.load_config(base)
    assert C.source_max_body_bytes(cfg, "github") == 786432                # explicit
    assert C.source_max_body_bytes(cfg, "gcal") == 786432                  # 1 MiB intake cap clamped
    cfg["routes"][0]["max_bytes"] = 1000
    cfg["sources"]["github"].pop("max_body_bytes")
    assert C.source_max_body_bytes(cfg, "github") == 1000


def test_route_resolution(base):
    cfg = C.load_config(base)
    assert C.resolve_route(cfg, "github", {"class": "ci", "repo": "o/r"}, "hermes:github") == \
        ("github-ci", "hermes:github-ci")
    assert C.resolve_route(cfg, "github", {"class": "pr"}, "hermes:github") == ("github-prs", "hermes:github-prs")
    assert C.resolve_route(cfg, "github", {"class": "other"}, "hermes:github") == ("github", "hermes:github")
    assert C.resolve_route(cfg, "gcal", {}, "hermes:ignored") == ("gcal", "script:gcal_delta")
    assert C.route_destination(cfg, "github-ci") == "hermes:github-ci"
    assert C.route_destination(cfg, "gcal") == "script:gcal_delta"
    assert C.destination_type(cfg, "audit") == "http" and C.destination_max_body_bytes(cfg, "audit") == 1048576
    assert C.destination_max_body_bytes(cfg, "script:gcal_delta") is None
    assert C.destination_max_body_bytes(cfg, "hermes:x") == 1048576


# ---- secrets --------------------------------------------------------------------------------------
def test_secret_refs_resolve_at_use(tmp_path, monkeypatch, secret_file):
    assert C.resolve_secret({"file": secret_file}) == SENTINEL
    with open(secret_file, "w") as f:
        f.write("rotated")
    assert C.resolve_secret({"file": secret_file}) == "rotated"           # never cached
    monkeypatch.setenv("HS_TEST_SECRET", "from-env")
    assert C.resolve_secret({"env": "HS_TEST_SECRET"}) == "from-env"
    envf = tmp_path / "x.env"
    envf.write_text("# comment\nexport A='one'\nB=\"two\"\nbad line\nC=three\n")
    assert C.read_env_file(str(envf)) == {"A": "one", "B": "two", "C": "three"}
    assert C.resolve_secret({"env_file": str(envf), "key": "B"}) == "two"
    for bad in ("literal-value", {"file": "a", "env": "b"}, {"key": "x"}, 5):
        with pytest.raises(C.SecretError):
            C.resolve_secret(bad)
    for missing in ({"file": str(tmp_path / "none")}, {"env": "HS_NOT_SET_ANYWHERE"},
                    {"env_file": str(envf), "key": "Z"}):
        with pytest.raises(C.SecretError) as e:
            C.resolve_secret(missing)
        assert SENTINEL not in str(e.value)


# ---- check: a clean config, then every rule -------------------------------------------------------
def test_clean_config_passes(base):
    rep = run_check(C.load_config(base))
    assert rep.errors == []
    assert not has(rep.warnings, "quiet_s")


def test_unknown_and_removed_keys(base):
    cfg = C.load_config(base)
    cfg["bogus"] = 1
    cfg["intake"]["lisen"] = "x"
    cfg["limits"]["max_pending_batches"] = 10
    cfg["limits"]["max_db_bytes"] = 10
    cfg["limits"]["plugin_error_streak"] = 3
    cfg["retention"]["archive_dir"] = "/x"
    cfg["dispatch"]["poison_after"] = 3
    cfg["dispatch"]["breaker"]["trip"] = 3
    cfg["sources"]["github"]["rate_per_min"] = 5
    cfg["sources"]["github"]["delay_s"] = 5
    cfg["routes"][0]["paused"] = True
    cfg["destinations"]["audit"]["idempotency"] = "per-batch"
    cfg["destinations"]["audit"]["rate_per_min"] = 5
    rep = run_check(cfg)
    for k in ("bogus", "intake.lisen", "dispatch.breaker.trip"):
        assert has(rep.errors, k, "unknown key"), k
    for k in ("limits.max_pending_batches", "limits.max_db_bytes", "limits.plugin_error_streak",
              "retention.archive_dir", "dispatch.poison_after", "sources.github.rate_per_min",
              "sources.github.delay_s", "routes[0].paused", "destinations.audit.idempotency",
              "destinations.audit.rate_per_min"):
        assert has(rep.errors, k, "removed in v2"), k


def test_plugin_config_keys_are_allowed(base):
    cfg = C.load_config(base)
    cfg["sources"]["github"]["callback_url"] = "x"
    assert has(run_check(cfg).errors, "callback_url", "unknown key")
    plugins = dict(PLUGINS, github=plugin(config_keys=("callback_url",)))
    assert run_check(cfg, plugins).errors == []


def test_quiet_greater_than_max_warns(base):
    cfg = C.load_config(base)
    cfg["routes"][1]["quiet_s"] = 900
    rep = run_check(cfg)
    assert rep.ok and has(rep.warnings, "route github-prs", "quiet_s 900 > max_s 600")


def test_max_bytes_bounded_by_destination_cap(base):
    cfg = C.load_config(base)
    cfg["routes"][0]["max_bytes"] = 786433                                 # > 0.75 x 1 MiB (Hermes default)
    cfg["sources"]["github"]["max_body_bytes"] = 1000
    rep = run_check(cfg)
    assert has(rep.errors, "route github-ci", "exceeds 0.75 x hermes:github-ci")
    cfg["routes"][0]["max_bytes"] = 786432
    assert run_check(cfg).ok
    cfg["hermes"]["max_body_bytes"] = 500000                               # gateway accepts less
    assert has(run_check(cfg).errors, "exceeds 0.75 x hermes:github-ci max_body_bytes (500000)")
    cfg = C.load_config(base)
    cfg["destinations"]["hermes:github-prs"] = {"max_body_bytes": 100000}
    cfg["sources"]["github"]["max_body_bytes"] = 1000
    assert has(run_check(cfg).errors, "route github-prs", "exceeds 0.75 x hermes:github-prs")


def test_source_body_cap_must_fit_route(base):
    cfg = C.load_config(base)
    cfg["routes"][1]["max_bytes"] = 100000
    rep = run_check(cfg)
    assert has(rep.errors, "route github-prs", "source max_body_bytes 786432 exceeds the route's max_bytes 100000")
    cfg["sources"]["github"]["max_body_bytes"] = 100000
    assert run_check(cfg).ok


def test_dedup_window_longer_than_retention_warns(base):
    cfg = C.load_config(base)
    cfg["sources"]["gcal"]["dedup_window_s"] = 8 * 86400
    rep = run_check(cfg)
    assert rep.ok and has(rep.warnings, "sources.gcal.dedup_window_s", "exceeds retention.delivered_days")


def test_auth_headers_refused_from_keep_headers(base):
    cfg = C.load_config(base)
    cfg["sources"]["github"]["keep_headers"] = ["X-GitHub-Event", "x-hub-signature-256"]
    assert has(run_check(cfg).errors, "sources.github.keep_headers", "x-hub-signature-256", "auth header")
    bad_default = dict(PLUGINS, github=plugin(keep_headers=("X-Hub-Signature-256",)))
    assert has(run_check(C.load_config(base), bad_default).errors, "auth header")


def test_hermes_events_and_coalesce_refused(base, subs_path):
    cfg = C.load_config(base)
    write_subs(subs_path, {"github-ci": {"secret": "s", "events": ["workflow_run"]},
                           "github-prs": {"secret": "s", "coalesce": {"window_s": 30}}})
    rep = run_check(cfg)
    assert has(rep.errors, "hermes:github-ci", "'events'")
    assert has(rep.errors, "hermes:github-prs", "'coalesce'")
    assert has(rep.warnings, "hermes:github", "no such route")             # the implicit route
    os.remove(subs_path)
    assert has(run_check(cfg).errors, "hermes.subscriptions_path", "cannot read")


def test_secret_checks(base, tmp_path, monkeypatch, secret_file):
    cfg = C.load_config(base)
    cfg["sources"]["github"]["secrets"]["secret"] = "a-literal"
    cfg["sources"]["gcal"]["secrets"]["secret"] = {"file": str(tmp_path / "missing")}
    cfg["sources"]["gcal"]["secrets"]["other"] = {"env": "HS_UNSET_VAR"}
    rep = run_check(cfg)
    assert has(rep.errors, "sources.github.secrets.secret", "never a literal")
    assert has(rep.errors, "sources.gcal.secrets.secret", "not readable")
    assert has(rep.errors, "sources.gcal.secrets.other", "HS_UNSET_VAR is not set")
    assert has(rep.warnings, "sources.gcal.secrets.other", "do not rotate")
    assert not any(SENTINEL in m for m in rep.errors + rep.warnings)
    cfg = C.load_config(base)
    os.chmod(secret_file, 0o644)
    assert has(run_check(cfg).warnings, "readable by group or others")
    del cfg["sources"]["github"]["secrets"]["secret"]
    assert has(run_check(cfg).errors, "sources.github.secrets.secret", "required by the plugin")
    assert run_check(C.load_config(base), check_secrets=False).ok


def test_plugin_import_and_action(base):
    cfg = C.load_config(base)
    rep = run_check(cfg, plugins={"gcal": PLUGINS["gcal"]})
    assert has(rep.errors, "sources.github", "cannot import plugin")
    cfg["sources"]["gcal"].pop("action")
    rep = run_check(cfg, plugins=dict(PLUGINS, gcal=plugin(action=None, auth_headers=())))
    assert has(rep.errors, "sources.gcal", "no action")
    cfg["sources"]["gcal"]["action"] = "quarantine"
    assert has(run_check(cfg).errors, "sources.gcal.action", "reserved")
    cfg["sources"]["gcal"]["action"] = "nowhere"
    assert has(run_check(cfg).errors, "sources.gcal.action", "unknown destination")
    assert C.check(C.load_config(base), load_plugin=None).ok               # plugin checks skipped


def test_sender_retries_false_is_reported(base):
    rep = run_check(C.load_config(base), dict(PLUGINS, github=plugin(sender_retries=False)))
    assert rep.ok and has(rep.warnings, "sources.github", "does not retry")


def test_route_rules(base):
    cfg = C.load_config(base)
    cfg["routes"].append({"name": "github-ci", "source": "github", "destination": "hermes:x"})
    cfg["routes"].append({"name": "orphan", "source": "nosuch", "destination": "hermes:x"})
    cfg["routes"].append({"name": "quarantine", "source": "github"})
    cfg["routes"].append({"name": "m", "source": "github", "match": {"a": [1]}, "destination": "bad dest"})
    cfg["routes"].append({"source": "github"})
    cfg["routes"].append({"name": "w", "source": "github", "max_events": 0})
    rep = run_check(cfg)
    assert has(rep.errors, "routes.github-ci", "duplicate")
    assert has(rep.errors, "routes.orphan.source", "not a configured source")
    assert has(rep.errors, "routes.quarantine", "reserved")
    assert has(rep.errors, "routes.m.match", "scalar")
    assert has(rep.errors, "routes.m.destination", "unknown destination")
    assert has(rep.errors, "routes[6].name", "required")
    assert has(rep.errors, "routes.w.max_events", ">= 1")


def test_destination_rules(base):
    cfg = C.load_config(base)
    d = cfg["destinations"]
    d["plainname"] = {"url": "https://x.invalid"}
    d["quarantine"] = {"type": "http"}
    d["insecure"] = {"type": "http", "url": "http://x.invalid", "secret": d["audit"]["secret"]}
    d["insecure-ok"] = {"type": "http", "url": "http://x.invalid", "secret": d["audit"]["secret"],
                        "allow_insecure": True}
    d["nosecret"] = {"type": "http", "url": "https://x.invalid"}
    d["weird"] = {"type": "carrier-pigeon"}
    d["script:noargv"] = {"argv": []}
    d["script:relcwd"] = {"argv": ["x"], "cwd": "relative/dir"}
    d["hermes:github-ci"] = {"max_inflight": 0, "argv": ["x"]}
    cfg["sources"]["gcal"]["action"] = "script:undefined"
    rep = run_check(cfg)
    assert has(rep.errors, "destinations.plainname.type", "required")
    assert has(rep.errors, "destinations.quarantine", "reserved")
    assert has(rep.errors, "destinations.insecure.url", "https:// required")
    assert not has(rep.errors, "destinations.insecure-ok")
    assert has(rep.errors, "destinations.nosecret.secret", "required")
    assert has(rep.errors, "destinations.weird.type", "unknown destination type")
    assert has(rep.errors, "destinations.script:noargv.argv")
    assert has(rep.errors, "destinations.script:relcwd.cwd", "absolute")
    assert has(rep.errors, "destinations.hermes:github-ci.max_inflight", ">= 1")
    assert has(rep.errors, "destinations.hermes:github-ci.argv", "unknown key")
    assert has(rep.errors, "script:undefined", "no [destinations")


def test_span_and_number_rules(base):
    cfg = C.load_config(base)
    cfg["spans"]["half"] = {"quiet_s": 5}
    cfg["sources"]["gcal"]["span"] = "nosuch"
    cfg["dispatch"]["parallel"] = 0
    cfg["retention"]["delivered_days"] = -1
    cfg["coalesce"]["max_events"] = 0
    cfg["alerts"]["argv"] = []
    cfg["intake"]["max_body_bytes"] = "big"
    rep = run_check(cfg)
    assert has(rep.errors, "spans.half.max_s", "required")
    assert has(rep.errors, "sources.gcal.span", "unknown span preset")
    assert has(rep.errors, "dispatch.parallel", ">= 1")
    assert has(rep.errors, "retention.delivered_days", ">= 0")
    assert has(rep.errors, "coalesce.max_events", ">= 1")
    assert has(rep.errors, "alerts.argv")
    assert has(rep.errors, "intake.max_body_bytes")
