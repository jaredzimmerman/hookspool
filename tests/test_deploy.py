"""Deployment files (contract C6): units, nginx, config example, README and the GitHub sweep.

None of nginx, Docker or systemd-analyze is needed: the nginx and unit files are linted here, and
``nginx -t`` / ``systemd-analyze verify`` also run when the tool exists. The config example goes through
the real ``hookspool check`` with its placeholder paths moved under a temp dir.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib

import pytest

from hookspool import __main__ as cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY = os.path.join(ROOT, "deploy")


OPS = os.path.join(os.path.dirname(DEPLOY), "docs", "operations")


def read(*p: str) -> str:
    with open(os.path.join(DEPLOY, *p), encoding="utf-8") as f:
        return f.read()


def read_ops(name: str) -> str:
    """A page of the operations guide, which holds the instructions for the files in deploy/."""
    with open(os.path.join(OPS, name), encoding="utf-8") as f:
        return f.read()


CONFIG = tomllib.loads(read("config.example.toml"))


# ---- nginx ----------------------------------------------------------------------------------
def nginx_tokens(text: str):
    """Strip comments, split into (statement, block-or-None) pairs; raise on unbalanced braces or a
    missing semicolon. A deliberately small parser: enough to catch the mistakes that matter."""
    text = re.sub(r"#[^\n]*", "", text)
    pos = 0

    def parse_block(depth: int):
        nonlocal pos
        out = []
        buf = ""
        while pos < len(text):
            c = text[pos]
            pos += 1
            if c == ";":
                out.append((buf.strip(), None))
                buf = ""
            elif c == "{":
                head = buf.strip()
                buf = ""
                out.append((head, parse_block(depth + 1)))
            elif c == "}":
                assert depth > 0, "unmatched }"
                assert not buf.strip(), f"missing ; before }}: {buf.strip()!r}"
                return out
            else:
                buf += c
        assert depth == 0, "unclosed {"
        assert not buf.strip(), f"missing ; at end: {buf.strip()!r}"
        return out

    return parse_block(0)


def find(block, name):
    return [(s, b) for s, b in block if s.split()[0] == name]


def directives(block, name):
    return [s.split()[1:] for s, b in block if b is None and s.split()[0] == name]


@pytest.fixture(scope="module")
def nginx():
    return nginx_tokens(read("nginx.example.conf"))


def servers(nginx):
    return [b for s, b in nginx if s == "server"]


def https_server(nginx):
    (srv,) = [b for b in servers(nginx) if directives(b, "ssl_certificate")]
    return srv


def test_nginx_parses_and_has_the_required_settings(nginx):
    srv = https_server(nginx)
    assert directives(srv, "client_max_body_size") == [["1m"]]
    assert directives(srv, "proxy_request_buffering") == [["on"]]
    assert directives(srv, "proxy_next_upstream") == [["off"]]
    assert directives(srv, "proxy_http_version") == [["1.1"]]
    assert directives(srv, "ssl_protocols") == [["TLSv1.2", "TLSv1.3"]]
    # the port-80 server only answers ACME and redirects
    (plain,) = [b for b in servers(nginx) if not directives(b, "ssl_certificate")]
    assert [s.split()[1] for s, _ in find(plain, "location")] == ["/.well-known/acme-challenge/", "/"]


def test_nginx_drops_idle_upstream_connections_before_the_intake_does(nginx):
    from hookspool import intake
    (up,) = [b for sel, b in nginx if sel == "upstream hookspool_intake"]
    (ka,) = directives(up, "keepalive_timeout")
    assert ka[0].endswith("s") and float(ka[0][:-1]) < intake.REQUEST_TIMEOUT_S


def test_nginx_body_limit_matches_the_intake_limit(nginx):
    mb = CONFIG["intake"]["max_body_bytes"]
    assert mb == 1048576
    assert directives(https_server(nginx), "client_max_body_size") == [["1m"]]


def test_effective_source_cap_is_768_kib_and_the_docs_say_so():
    from hookspool import bobbin_loader
    from hookspool import config as hc
    ex = bobbin_loader.expand(hc.merge(hc.DEFAULTS, CONFIG))     # the [bobbins.*] instances as the sources they become
    caps = {hc.source_max_body_bytes(ex, name) for name in ex["sources"] if "max_body_bytes" not in ex["sources"][name]}
    assert caps == {786432}, caps
    row = [ln for ln in read_ops("sweeps.md").splitlines() if ln.startswith("| **GitHub payload over")][0]
    assert "768 KiB" in row and "1 MiB by default" not in row
    assert "768 KiB" in read("nginx.example.conf")


def test_nginx_every_location_rate_limits_with_a_defined_zone_and_a_real_upstream(nginx):
    zones = {t.removeprefix("zone=").split(":")[0] for d in directives(nginx, "limit_req_zone")
             for t in d if t.startswith("zone=")}
    assert zones, "no limit_req_zone"
    upstreams = {s.split()[1] for s, _ in find(nginx, "upstream")}
    assert directives(nginx, "limit_req_status") == [["429"]]
    srv = https_server(nginx)
    sources = []
    for head, body in find(srv, "location"):
        path = head.split()[-1]
        if path == "/":
            assert directives(body, "return") == [["404"]]            # nothing is served by default
            continue
        lr = directives(body, "limit_req")
        assert len(lr) == 1 and lr[0][0].removeprefix("zone=") in zones, path
        (pp,) = directives(body, "proxy_pass")
        assert re.match(r"http://(\w+)(/hooks/\w+)?$", pp[0]) and pp[0].split("/")[2] in upstreams, path
        assert head.split()[1] == "=", f"{path}: exact match only"
        # the location must not set proxy_set_header: that would stop it inheriting the server's set
        assert not directives(body, "proxy_set_header"), path
        target = pp[0].partition("/hooks/")[2] or path.removeprefix("/hooks/")
        sources.append(target)
    # one location per source in the config example (github has two paths, one source)
    assert set(sources) == set(CONFIG.get("sources", {})) | set(CONFIG.get("bobbins", {}))


def test_nginx_route_hints_come_from_the_path_and_match_the_config_routes(nginx):
    (mp,) = find(nginx, "map")
    entries = {tuple(s.split())[0]: tuple(s.split())[1] for s, b in mp[1]}
    srv = https_server(nginx)
    assert ["X-Hookspool-Route", "$hookspool_route"] in directives(srv, "proxy_set_header")
    hinted = {v for k, v in entries.items() if k != "default"}
    wanted = {r["match"]["route_hint"] for r in CONFIG["routes"] if "route_hint" in r.get("match", {})}
    assert hinted == wanted == {"ci", "prs"}
    # each hinted path has a location
    paths = {h.split()[-1] for h, _ in find(srv, "location")}
    assert {k for k in entries if k != "default"} <= paths


def test_nginx_never_logs_the_query_string(nginx):
    text = re.sub(r"#[^\n]*", "", read("nginx.example.conf"))
    fmt = re.search(r"log_format\s+hookspool\s+(.*?);", text, re.S).group(1)
    used = set(re.findall(r"\$\w+", fmt))
    assert "$uri" in used
    assert not used & {"$request", "$request_uri", "$args", "$query_string", "$is_args"}, used
    assert any(d[1:] == ["hookspool"] for d in directives(https_server(nginx), "access_log"))


def test_nginx_the_gmail_push_token_reaches_no_nginx_log(nginx):
    # access: the port-80 server must not inherit the default `combined` log (it writes $request)
    (plain,) = [b for b in servers(nginx) if not directives(b, "ssl_certificate")]
    assert directives(plain, "access_log") == [["off"]]
    # error log: nginx writes the request line and upstream URI (query string included) on every limit_req
    # rejection and every 502; the gmail location keeps those out with its own level
    srv = https_server(nginx)
    (gm,) = [b for h, b in find(srv, "location") if h.split()[-1] == "/hooks/gmail"]
    (el,) = directives(gm, "error_log")
    assert el[1] == "crit" and el[0].startswith("/"), el
    (lv,) = directives(gm, "limit_req_log_level")
    assert lv[0] in ("info", "notice", "warn"), lv      # below the crit threshold of the location's error_log
    # nothing at server or http level turns the token-bearing lines back on for that path
    assert not directives(srv, "error_log") or all(d[1] in ("crit", "emerg", "alert") for d in directives(srv, "error_log"))


def test_nginx_does_not_expose_healthz_or_metrics(nginx):
    text = re.sub(r"#[^\n]*", "", read("nginx.example.conf"))
    assert "healthz" not in text and "metrics" not in text


@pytest.mark.skipif(not shutil.which("nginx"), reason="nginx not installed")
def test_nginx_t(tmp_path):
    conf = tmp_path / "nginx.conf"
    cert = tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(tmp_path / "k.pem"),
                    "-out", str(cert), "-subj", "/CN=hooks.example.com", "-days", "1"], check=True,
                   capture_output=True)
    body = read("nginx.example.conf").replace("/etc/letsencrypt/live/hooks.example.com/fullchain.pem", str(cert)) \
        .replace("/etc/letsencrypt/live/hooks.example.com/privkey.pem", str(tmp_path / "k.pem")) \
        .replace("/var/log/nginx/hookspool.access.log", str(tmp_path / "a.log"))
    # every other log path the example names (per-location error logs) goes under tmp_path too: the
    # runner may not write /var/log/nginx, and nginx -t opens each log file it is given
    body = re.sub(r"/var/log/nginx/([A-Za-z0-9._-]+)", lambda m: str(tmp_path / m.group(1)), body)
    # nginx -t binds the listen ports; a non-root runner cannot bind 80/443, so test on high ports
    body = re.sub(r"(listen\s+(?:\[::\]:)?)(80|443)\b", lambda m: m.group(1) + {"80": "18080", "443": "18443"}[m.group(2)], body)
    conf.write_text(f"pid {tmp_path}/n.pid; error_log {tmp_path}/e.log; events {{}}\nhttp {{\n{body}\n}}\n")
    r = subprocess.run(["nginx", "-t", "-c", str(conf)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ---- systemd units --------------------------------------------------------------------------
def unit(name: str) -> dict[str, dict[str, list[str]]]:
    out: dict[str, dict[str, list[str]]] = {}
    sect = None
    for ln in read(name).splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if ln.startswith("["):
            sect = out.setdefault(ln.strip("[]"), {})
            continue
        k, _, v = ln.partition("=")
        assert sect is not None and _ == "=", f"{name}: bad line {ln!r}"
        sect.setdefault(k.strip(), []).append(v.strip())
    return out


@pytest.fixture(params=["hookspool-intake.service", "hookspool-dispatch.service"])
def svc(request):
    return request.param, unit(request.param)


def subcommands():
    ap = cli._parser()
    (sp,) = [a for a in ap._actions if a.dest == "cmd"]
    return set(sp.choices)


def test_units_restart_check_reload_and_hardening(svc):
    name, u = svc
    s = u["Service"]
    assert s["Restart"] == ["always"] and s["RestartSec"] == ["2"]
    assert u["Unit"]["StartLimitIntervalSec"] == ["0"]          # keeps retrying while `check` fails (README 5)
    assert s["NoNewPrivileges"] == ["yes"] and s["ProtectSystem"] == ["strict"]
    assert s["ExecStartPre"] == ["/usr/local/bin/hookspool check"]
    assert s["ExecReload"] == ["/usr/local/bin/hookspool reload"]
    cmd = name.split("-")[1].split(".")[0]
    assert s["ExecStart"] == [f"/usr/local/bin/hookspool {cmd}"]
    for c in ("check", "reload", cmd):
        assert c in subcommands()
    assert u["Install"]["WantedBy"] == ["multi-user.target"]
    assert s["Type"] == ["simple"] and s["User"] and s["Group"]
    assert s["UMask"] == ["0077"]
    assert "Description" in u["Unit"]
    assert "MemoryDenyWriteExecute" not in s          # untested with cffi/cryptography; leave it off


def rw(u):
    return set(u["Service"]["ReadWritePaths"][0].split())


def test_dispatch_read_write_paths_cover_home_and_every_script_state_dir():
    home = CONFIG["spool"]["home"]
    state_dirs = [d["state_dir"] for n, d in CONFIG["destinations"].items() if n.startswith("script:") and "state_dir" in d]
    paths = rw(unit("hookspool-dispatch.service"))
    # a bobbin's processor keeps its state in <home>/bobbins/<instance>, below the home that is listed
    assert CONFIG["bobbins"] and not any(b.get("state_dir") for b in CONFIG["bobbins"].values())
    assert home in paths
    for d in state_dirs:
        assert d in paths, f"{d} missing from ReadWritePaths"
    assert rw(unit("hookspool-intake.service")) == {home}
    for u in (unit("hookspool-dispatch.service"), unit("hookspool-intake.service")):
        assert f"HOOKSPOOL_HOME={home}" in u["Service"]["Environment"]


def test_only_the_dispatcher_refuses_to_restart_on_exit_3():
    d = unit("hookspool-dispatch.service")["Service"]
    assert d["RestartPreventExitStatus"] == ["3"]              # exit 3 = lock held (ARCHITECTURE section 16)
    assert "RestartPreventExitStatus" not in unit("hookspool-intake.service")["Service"]


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd-analyze not installed")
def test_systemd_analyze_verify(tmp_path):
    for n in ("hookspool-intake.service", "hookspool-dispatch.service"):
        shutil.copy(os.path.join(DEPLOY, n), tmp_path / n)
    r = subprocess.run(["systemd-analyze", "verify", *(str(tmp_path / n) for n in os.listdir(tmp_path))],
                       capture_output=True, text=True)
    # the hookspool binary and the User do not exist on a CI box; anything else is a real error
    bad = [ln for ln in r.stderr.splitlines()
           if ln and "/usr/local/bin/hookspool" not in ln and "User" not in ln and "Group" not in ln]
    assert not bad, bad


# ---- config example -------------------------------------------------------------------------
@pytest.fixture
def placed(tmp_path):
    """The config example with every placeholder path moved under tmp_path, secrets created."""
    text = read("config.example.toml")
    etc, var = tmp_path / "etc", tmp_path / "var"
    subs = tmp_path / "subs.json"
    text = text.replace("/etc/hookspool", str(etc)).replace("/var/lib/hookspool", str(var)) \
        .replace("~/.hermes/webhook_subscriptions.json", str(subs))
    cfg = tomllib.loads(text)
    (etc / "secrets").mkdir(parents=True)
    (etc / "google").mkdir(parents=True)
    for s in [*cfg.get("sources", {}).values(), *cfg["bobbins"].values()]:
        for ref in s.get("secrets", {}).values():
            if "file" in ref:
                open(ref["file"], "w").write("test-secret\n")
                os.chmod(ref["file"], 0o600)
            else:
                open(ref["env_file"], "w").write("PLAID_CLIENT_ID=a\nPLAID_SECRET=b\n")
                os.chmod(ref["env_file"], 0o600)
    # the Hermes side, as an operator has it after step 5 of the README: no events, no coalesce
    names = {r["destination"] for r in cfg["routes"]} | {s["action"] for s in cfg.get("sources", {}).values()} \
        | {d for b in cfg["bobbins"].values() for d in b["outputs"].values()}
    subs.write_text(json.dumps({n.split(":", 1)[1]: {"secret": "s", "prompt": "x"}
                                for n in names if n.startswith("hermes:")}))
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path, cfg, tmp_path


def test_config_example_passes_hookspool_check(placed, capsys, monkeypatch):
    path, _cfg, tmp = placed
    monkeypatch.setenv("HOOKSPOOL_HOME", str(tmp / "var"))
    assert cli.main(["--config", str(path), "check"]) == 0
    err = capsys.readouterr().err
    lines = [ln for ln in err.splitlines() if ln.strip()]
    assert all(ln.startswith("warning:") for ln in lines), lines
    assert len(lines) == 1 and "sources.github: the sender does not retry" in lines[0]    # the one expected warning


def test_config_example_check_catches_a_hermes_events_filter(placed, capsys, monkeypatch):
    """The README tells operators to strip `events` from fed routes first; check must hold them to it."""
    path, cfg, tmp = placed
    subs = tmp / "subs.json"
    d = json.loads(subs.read_text())
    d["github-ci"]["events"] = ["workflow_run"]
    subs.write_text(json.dumps(d))
    monkeypatch.setenv("HOOKSPOOL_HOME", str(tmp / "var"))
    assert cli.main(["--config", str(path), "check"]) == 2


def test_config_example_every_source_is_covered_by_the_readme_sweep_table():
    readme = read_ops("sweeps.md")
    table = readme.split("# Sweeps\n")[1].split("### Known limits")[0]
    rows = {ln.split("|")[1].strip().lower(): ln for ln in table.splitlines() if ln.startswith("| ") and "---" not in ln}
    for src in [*CONFIG.get("sources", {}), *CONFIG["bobbins"]]:
        key = {"gmail": "gmail (pub/sub)", "meetings": "zoom"}.get(src, src)
        assert key in rows, f"no sweep row for {src}"
        cells = [c.strip() for c in rows[key].split("|")[1:-1]]
        assert len(cells) == 7 and all(cells), rows[key]
        # the example ships 0 everywhere (the rollout order in running.md: a source that was never moved would alert
        # "silent" on day one); the value to set at cutover is the bobbin's recommendation, named in the sweeps cell
        if src in CONFIG["bobbins"]:
            bobbin = CONFIG["bobbins"][src]["bobbin"]                 # <vendor>.<product>: hookspool/bobbins/<v>/<p>/
            with open(os.path.join(ROOT, "hookspool", "bobbins", *bobbin.split("."), "bobbin.toml"), "rb") as f:
                later = tomllib.load(f)["source"].get("expect_every_s")
            if later is None:                    # the bobbin recommends no silence limit: the cell says 0
                assert CONFIG["bobbins"][src].get("expect") is not True, src
                assert cells[4].startswith("`0`"), f"{src}: README says {cells[4]!r}, the bobbin recommends none"
            else:
                assert CONFIG["bobbins"][src].get("expect") is False, src
                assert f"`{later}`" in cells[4], f"{src}: README says {cells[4]!r}, the bobbin recommends {later}"
        else:
            assert CONFIG["sources"][src].get("expect_every_s", 0) == 0, src
    assert "removed" in table.lower() or "none" in table.lower()


# ---- README ---------------------------------------------------------------------------------
def test_operations_pages_have_the_required_sections_and_rows():
    running, drills = read_ops("running.md"), read_ops("drills.md")
    sweeps, upgrading = read_ops("sweeps.md"), read_ops("upgrading.md")
    r = "\n".join((running, drills, sweeps, upgrading, read_ops("day-two.md")))
    for page_text, h in ((running, "## Install"), (running, "## Rollout order and rollback"),
                         (drills, "## Gateway-down drill"), (sweeps, "# Sweeps"),
                         (sweeps, "### Known limits"), (upgrading, "# Upgrading")):
        assert h in page_text, h
    # rollout: GitHub first, every step names its rollback
    steps = [ln for ln in running.split("## Rollout")[1].split("Per-step notes")[0].splitlines() if re.match(r"\| \d\.", ln)]
    assert [re.match(r"\| \d\. (\w+)", s).group(1) for s in steps] == \
        ["install", "GitHub", "Plaid", "Gmail", "Calendar", "Trello", "Zoom", "retire"]
    assert all(len(s.split("|")) == 6 and s.split("|")[4].strip() for s in steps)    # a rollback in every row
    # the two documented limits (PLAN T7)
    assert re.search(r"GitHub payload over the source `max_body_bytes`.*413.*not retry|"
                     r"GitHub payload over the source.*\*\*413\*\*", r, re.S)
    assert "does not retry it" in r and "raise the limit end to end".lower() in r.lower()
    assert "one key fetch a minute" in r and "random-`kid`" in r and "`limit_req`" in r
    assert "token bucket" in r and "later option" in r
    # sender retries
    assert re.search(r"\| GitHub \|[^\n]*\*\*No\.\*\*", r)


def test_operations_cli_commands_exist():
    r = "\n".join(read_ops(n) for n in ("running.md", "drills.md", "sweeps.md", "upgrading.md", "day-two.md"))
    code = "\n".join(re.findall(r"```\w*\n(.*?)```", r, re.S) + re.findall(r"`([^`\n]+)`", r))
    cmds = set(re.findall(r"(?<![\w/.-])(?<!-[ugo] )hookspool[ \t]+(?:--?[\w-]+[ \t]+)*([a-z]+)\b", code))
    assert {"status", "list", "issues", "enqueue", "reload", "migrate", "check", "pause", "export", "replay", "drop",
            "source"} <= cmds
    assert cmds <= subcommands(), cmds - subcommands()


# Generic patterns only. Deployment-specific terms (an operator's account, person or host names) must not be
# committed, so they come from a gitignored file, one term per line (`#` comments), or from the environment
# (HOOKSPOOL_DENYLIST, comma-separated). With neither, only the generic part of the scan runs.
GENERIC_DENY = [r"/root/", r"chat[_ ]?id"]


def local_deny_terms() -> list[str]:
    terms = [t.strip() for t in os.environ.get("HOOKSPOOL_DENYLIST", "").split(",")]
    path = os.environ.get("HOOKSPOOL_DENYLIST_FILE") or os.path.join(DEPLOY, "local", "denylist.txt")
    try:
        with open(path, encoding="utf-8") as fh:
            terms += [ln.split("#", 1)[0].strip() for ln in fh]
    except FileNotFoundError:
        pass
    return [t for t in terms if t]


def deny_regex(terms: list[str]) -> re.Pattern:
    return re.compile("|".join(GENERIC_DENY + [re.escape(t) for t in terms]), re.I)


def test_local_denylist_is_read_from_the_env_and_the_gitignored_file(tmp_path, monkeypatch):
    f = tmp_path / "denylist.txt"
    f.write_text("# operator terms\nalpha-host\n\nbeta  # trailing comment\n", encoding="utf-8")
    monkeypatch.setenv("HOOKSPOOL_DENYLIST_FILE", str(f))
    monkeypatch.setenv("HOOKSPOOL_DENYLIST", "gamma, ,delta.x")
    terms = local_deny_terms()
    assert sorted(terms) == ["alpha-host", "beta", "delta.x", "gamma"]
    rx = deny_regex(terms)
    assert rx.search("see ALPHA-HOST here") and rx.search("/root/.x") and not rx.search("deltaYx")
    monkeypatch.setenv("HOOKSPOOL_DENYLIST_FILE", str(tmp_path / "absent.txt"))
    monkeypatch.delenv("HOOKSPOOL_DENYLIST")
    assert local_deny_terms() == []
    # the default file is under deploy/local/, which git ignores
    out = subprocess.run(["git", "-C", ROOT, "check-ignore", "-q", "deploy/local/denylist.txt"])
    if out.returncode not in (0, 1):
        pytest.skip("not a git checkout")
    assert out.returncode == 0, "deploy/local/ must stay gitignored"


def test_deploy_files_carry_no_hostnames_ips_or_account_names():
    deny = deny_regex(local_deny_terms())
    allowed_hosts = re.compile(r"(^|\.)(example\.com|example\.test|example\.invalid|localhost|test|invalid)$|^127\.0\.0\.1$")
    bad = []
    for dp, dn, fn in os.walk(DEPLOY):
        dn[:] = [d for d in dn if d not in ("local", "__pycache__")]
        for f in fn:
            if f.endswith(".pyc"):
                continue
            text = open(os.path.join(dp, f), encoding="utf-8").read()
            rel = os.path.relpath(os.path.join(dp, f), DEPLOY)
            for ip in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text):
                if ip not in ("127.0.0.1", "0.0.0.0"):
                    bad.append((rel, ip))
            for host in re.findall(r"https?://([\w.-]+)", text):
                if not allowed_hosts.search(host) and host != "hookspool_intake":
                    bad.append((rel, host))
            for m in deny.finditer(text):
                bad.append((rel, m.group(0)))
    # the delta scripts name Google API hosts; those are protocol constants, not deployment values
    bad = [(r, h) for r, h in bad if not (r.startswith("scripts/") and ("google" in h or "googleapis" in h))]
    assert not bad, bad


# ---- GitHub redelivery sweep ----------------------------------------------------------------
@pytest.fixture(scope="module")
def sweep():
    spec = importlib.util.spec_from_file_location("github_redeliver", os.path.join(DEPLOY, "sweeps", "github_redeliver.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


NOW = 1_800_000_000.0


def iso(ago_h: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(NOW - ago_h * 3600, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def delivery(i, guid, status, ago_h=1.0, event="push"):
    return {"id": i, "guid": guid, "delivered_at": iso(ago_h), "status_code": status, "event": event}


class FakeGh:
    def __init__(self, deliveries, fail_post=()):
        self.deliveries, self.fail_post, self.calls = deliveries, set(fail_post), []

    def __call__(self, args, **_kw):
        self.calls.append(args)
        if args[:2] == ["api", "--paginate"]:
            half = len(self.deliveries) // 2
            return json.dumps(self.deliveries[:half]) + "\n" + json.dumps(self.deliveries[half:])   # two pages
        if args[:3] == ["api", "-X", "POST"]:
            did = int(args[3].split("/")[-2])
            if did in self.fail_post:
                raise self_error("gh api exited 1: HTTP 422")
            return "{}"
        raise AssertionError(args)


def self_error(msg):
    return sys.modules["github_redeliver_err"](msg)


@pytest.fixture
def run(sweep, monkeypatch, capsys):
    sys.modules["github_redeliver_err"] = sweep.GhError

    def _run(deliveries, *argv, fail_post=()):
        fake = FakeGh(deliveries, fail_post)
        monkeypatch.setattr(sweep, "gh", fake)
        rc = sweep.main(["--repo", "o/r", "--hook-id", "7", *argv], now=NOW)
        out, err = capsys.readouterr()
        posts = [int(c[3].split("/")[-2]) for c in fake.calls if c[:3] == ["api", "-X", "POST"]]
        return rc, out, err, posts, fake
    return _run


def test_sweep_redelivers_only_deliveries_that_never_got_a_2xx(run):
    ds = [delivery(1, "a", 200), delivery(2, "b", 503, ago_h=2), delivery(3, "c", 0, ago_h=3), delivery(4, "d", 413, ago_h=4)]
    rc, out, err, posts, fake = run(ds)
    assert rc == 0 and sorted(posts) == [2, 3]                  # the 413 would be refused again: not redelivered
    assert "2 without a 2xx" in out and "2 redelivered" in out and "1 not redelivered (413" in out
    assert fake.calls[0] == ["api", "--paginate", "repos/o/r/hooks/7/deliveries?per_page=100"]
    assert fake.calls[1][:4] == ["api", "-X", "POST", "repos/o/r/hooks/7/deliveries/2/attempts"]       # newest first


def test_sweep_does_not_redeliver_a_guid_that_a_redelivery_landed(run):
    ds = [delivery(1, "x", 502, ago_h=5), delivery(2, "x", 200, ago_h=4)]      # same GUID, second attempt landed
    assert run(ds)[3] == []


def test_sweep_uses_the_newest_attempt_of_a_guid_and_redelivers_it_once(run):
    ds = [delivery(1, "x", 502, ago_h=5), delivery(2, "x", 503, ago_h=3)]
    assert run(ds)[3] == [2]


def test_sweep_skips_pings_and_old_deliveries(run):
    ds = [delivery(1, "p", 500, event="ping"), delivery(2, "old", 500, ago_h=60), delivery(3, "new", 500, ago_h=47)]
    assert run(ds)[3] == [3]
    assert run(ds, "--since-hours", "72")[3] == [3, 2]


def test_sweep_max_dry_run_and_refusals(run):
    ds = [delivery(i, f"g{i}", 500, ago_h=i) for i in range(1, 6)]
    rc, out, _e, posts, _ = run(ds, "--max", "2")
    assert posts == [1, 2] and "3 left for the next run" in out                       # newest first
    rc, out, _e, posts, _ = run(ds, "--dry-run")
    assert posts == [] and out.count("would redeliver") == 5
    rc, out, err, posts, _ = run(ds, "--max", "2", fail_post=[1])
    assert rc == 0 and posts == [1, 2] and "1 refused" in out and "refused g1" in err


def test_sweep_exit_codes(sweep, monkeypatch, capsys):
    def boom(args, **_):
        raise sweep.GhError("gh api exited 4: auth")
    monkeypatch.setattr(sweep, "gh", boom)
    assert sweep.main(["--repo", "o/r", "--hook-id", "7"], now=NOW) == 3
    assert "sweep failed" in capsys.readouterr().err
    with pytest.raises(SystemExit) as e:
        sweep.main(["--repo", "nope", "--hook-id", "7"])
    assert e.value.code == 2


def test_sweep_talks_to_gh_with_the_right_argv(sweep, tmp_path, monkeypatch):
    """The real subprocess path, against a fake `gh` first on PATH."""
    log = tmp_path / "calls.log"
    fake = tmp_path / "bin" / "gh"
    fake.parent.mkdir()
    fake.write_text(f"""#!{sys.executable}
import json, sys
open({str(log)!r}, "a").write(json.dumps(sys.argv[1:]) + "\\n")
if "--paginate" in sys.argv:
    print(json.dumps([{{"id": 9, "guid": "g", "delivered_at": "{iso(1)}", "status_code": 502, "event": "push"}}]))
else:
    print("{{}}")
""")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    assert sweep.main(["--repo", "o/r", "--hook-id", "7"], now=NOW) == 0
    calls = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert calls == [["api", "--paginate", "repos/o/r/hooks/7/deliveries?per_page=100"],
                     ["api", "-X", "POST", "repos/o/r/hooks/7/deliveries/9/attempts"]]


def test_sweep_survives_missing_timestamps_and_non_json_output(sweep, run, monkeypatch, capsys):
    ds = [{"id": 1, "guid": "a", "status_code": 500, "event": "push"},                       # no delivered_at
          {"guid": "b", "delivered_at": iso(1), "status_code": 500, "event": "push"},        # no id
          {"id": 3, "guid": "c", "delivered_at": "yesterday", "status_code": 500},           # unreadable time
          delivery(4, "d", 502)]
    rc, out, err, posts, _ = run(ds)
    assert rc == 0 and posts == [4] and "3 skipped" in out
    monkeypatch.setattr(sweep, "gh", lambda args, **_: "<html>rate limited</html>")
    assert sweep.main(["--repo", "o/r", "--hook-id", "7"], now=NOW) == 3
    assert "not JSON" in capsys.readouterr().err


def test_sweep_parse_pages_handles_concatenated_arrays(sweep):
    assert sweep.parse_pages('[{"id": 1}]\n[{"id": 2}, {"id": 3}]\n[]') == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert sweep.parse_pages("") == []
