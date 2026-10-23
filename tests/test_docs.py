"""Documentation against the code (T8).

The reference pages are only worth reading if they cannot drift, so this module reads them back against the
program:

* docs/CLI.md has a section for every command, every option of every command and every exit code, and names
  nothing the parser does not have; every ``hookspool <command> --flag`` written in any page is real
* docs/CONFIGURATION.md has a row for every config key (section, source, route, destination, breaker), with the
  documented default equal to the code's, and no row for a key that does not exist; the span presets match
* docs/ADDING_A_SOURCE.md lists every template key, scheme and plugin name, and every ``v.<helper>`` it shows exists
* every ``toml`` block in the pages parses and passes the config checker's key validation
* the worked examples in ADDING_A_SOURCE.md and ADDING_A_DESTINATION.md and the README's quick-start config run
* relative links and anchors resolve; no page carries a hostname, an address or a deployment term
* README and CONTRIBUTING have the sections the project promises
* pyproject.toml is complete and ``python -m build`` (or ``uv build``) produces a wheel with the migrations in it
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sourcekit  # noqa: E402
from test_deploy import deny_regex, local_deny_terms  # noqa: E402

from hookspool import __main__ as cli  # noqa: E402
from hookspool import config as hs_config  # noqa: E402
from hookspool import verify as hs_verify  # noqa: E402
from hookspool.sources import template  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
PAGES = [os.path.join(ROOT, "README.md"), os.path.join(ROOT, "CONTRIBUTING.md"), os.path.join(ROOT, "CHANGELOG.md")] \
    + sorted(glob.glob(os.path.join(DOCS, "*.md")))
# ARCHITECTURE.md is the design contract and uses pseudo-commands and elided examples; the checks that run a
# page's examples cover the pages written to be followed.
FOLLOWED = [p for p in PAGES if os.path.basename(p) not in ("ARCHITECTURE.md", "CHANGELOG.md")]


def read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def page(name: str) -> str:
    return read(os.path.join(ROOT, name))


def _outside_fences(text: str) -> str:
    """``text`` with the inside of fenced code blocks blanked, same length, so offsets still line up."""
    return re.sub(r"```.*?```", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)


def section(text: str, heading: str, level: int = 2) -> str:
    """The body of the ``#`` * level heading ``heading`` up to the next heading of the same or higher level
    (a ``# comment`` line inside a code fence is not a heading)."""
    plain = _outside_fences(text)
    m = re.search(rf"^{'#' * level} {re.escape(heading)}\s*$", plain, re.M)
    assert m, f"no heading {'#' * level} {heading}"
    nxt = re.search(rf"^#{{1,{level}}} ", plain[m.end():], re.M)
    end = m.end() + nxt.start() if nxt else len(text)
    return text[m.end():end]


def ticks(text: str) -> set[str]:
    return set(re.findall(r"`([^`\n]+)`", text))


def table_rows(text: str) -> list[list[str]]:
    rows = []
    for ln in text.splitlines():
        if ln.startswith("|") and not re.match(r"^\|[\s:|-]+\|$", ln):
            rows.append([c.strip() for c in ln.strip().strip("|").split("|")])
    return rows


def first_cell_keys(text: str) -> set[str]:
    """Backticked names in the first column of every table row."""
    out: set[str] = set()
    for r in table_rows(text):
        out |= set(re.findall(r"`([^`]+)`", r[0]))
    return out


# ---- CLI --------------------------------------------------------------------------------------------------
def parser_commands() -> dict[str, argparse.ArgumentParser]:
    ap = cli._parser()
    sub = next(a for a in ap._actions if isinstance(a, argparse._SubParsersAction))
    return dict(sub.choices)


def options_of(p: argparse.ArgumentParser) -> set[str]:
    out = set()
    for a in p._actions:
        if a.help == argparse.SUPPRESS:
            continue
        out |= {o for o in a.option_strings if o not in ("-h", "--help")}
    return out


def positionals_of(p: argparse.ArgumentParser) -> list[str]:
    """What the page names for each positional: its choices when it has them (``test``), else its destination."""
    out = []
    for a in p._actions:
        if a.option_strings or isinstance(a, argparse._SubParsersAction):
            continue
        out += list(a.choices) if a.choices and a.dest == "subcmd" else [a.dest]
    return out


GLOBAL_OPTIONS = options_of(cli._parser())


def cli_sections() -> dict[str, str]:
    text = page("docs/CLI.md")
    return {m.group(1): s for m in re.finditer(r"^### hookspool (\w+)\s*$", text, re.M)
            for s in [section(text, f"hookspool {m.group(1)}", 3)]}


def test_cli_page_has_exactly_the_commands_the_parser_has():
    assert set(cli_sections()) == set(parser_commands())


@pytest.mark.parametrize("cmd", sorted(parser_commands()))
def test_cli_page_documents_every_option_and_positional(cmd):
    p = parser_commands()[cmd]
    sec = cli_sections()[cmd]
    named = ticks(sec)
    documented = {t.split()[0].split("=")[0] for t in named if t.startswith("--")}
    missing = sorted(options_of(p) - documented)
    assert not missing, f"docs/CLI.md hookspool {cmd}: undocumented options {missing}"
    for pos in positionals_of(p):
        assert pos in named, f"docs/CLI.md hookspool {cmd}: positional `{pos}` is not described"
    # and nothing documented that the command does not take
    extra = documented - options_of(p) - GLOBAL_OPTIONS
    assert not extra, f"docs/CLI.md hookspool {cmd}: documents options the command lacks: {sorted(extra)}"


def test_cli_page_global_options_environment_and_exit_codes():
    text = page("docs/CLI.md")
    head = section(text, "Global options and environment")
    for o in GLOBAL_OPTIONS:
        assert f"`{o} " in head or f"`{o}`" in head, o
    env = set(re.findall(r"HOOKSPOOL_[A-Z_]+", head))
    in_code = set()
    for f in glob.glob(os.path.join(ROOT, "hookspool", "**", "*.py"), recursive=True):
        in_code |= set(re.findall(r"HOOKSPOOL_[A-Z_]+", read(f)))
    # the two variables a script receives are documented with scripts, the rest here
    script_env = {"HOOKSPOOL_BATCH_ID", "HOOKSPOOL_DELIVERY_ID", "HOOKSPOOL_STATE_DIR"}
    assert in_code - script_env <= env, sorted((in_code - script_env) - env)
    assert env <= in_code, sorted(env - in_code)
    codes = section(text, "Exit codes")
    got = {int(r[0]): r[1] for r in table_rows(codes)[1:]}
    assert sorted(got) == [cli.EXIT_OK, cli.EXIT_NOTHING, cli.EXIT_CONFIG, cli.EXIT_LOCK] == [0, 1, 2, 3]


CODE_SPAN = re.compile(r"```[^\n]*\n(.*?)```|`([^`\n]+)`", re.S)
CMD_RE = re.compile(r"(?<![\w/.-])(?<!-[ugo] )(?<!from )hookspool((?:[ \t]+--(?:home|config)[ \t]+\S+|[ \t]+--json)*)[ \t]+([a-z]+)\b([^\n]*)")


def commands_in_code(text: str):
    for m in CODE_SPAN.finditer(text):
        for ln in (m.group(1) or m.group(2)).splitlines():
            for c in CMD_RE.finditer(ln):
                yield ln, c.group(2), c.group(3)


@pytest.mark.parametrize("path", FOLLOWED, ids=[os.path.basename(p) for p in FOLLOWED])
def test_every_command_and_option_written_in_a_page_is_real(path):
    cmds = parser_commands()
    for ln, cmd, rest in commands_in_code(read(path)):
        if cmd == "env":                # `sudo -u hookspool env HOOKSPOOL_HOME=... hookspool check`
            continue
        assert cmd in cmds, f"{os.path.basename(path)}: `hookspool {cmd}` in {ln.strip()!r}"
        rest = re.split(r"\s#|\s\||\s>|;|&&|\)|`", rest)[0]
        for flag in re.findall(r"(?<![\w-])(--[\w-]+)", rest):
            assert flag in options_of(cmds[cmd]) | GLOBAL_OPTIONS | {"--help"}, \
                f"{os.path.basename(path)}: `hookspool {cmd} {flag}` in {ln.strip()!r}"


# ---- configuration --------------------------------------------------------------------------------------------
def norm(v: str):
    """A documented default as a comparable value."""
    v = v.strip().strip("`").strip()
    if v in ("true", "false"):
        return v == "true"
    try:
        return float(v)
    except ValueError:
        pass
    try:
        return json.loads(v)
    except ValueError:
        return v


SECTIONS = ["spool", "intake", "dispatch", "coalesce", "limits", "retention", "alerts", "hermes"]


@pytest.mark.parametrize("name", SECTIONS)
def test_config_page_section_matches_the_key_table_and_defaults(name):
    sec = section(page("docs/CONFIGURATION.md"), f"`[{name}]`")
    # the [dispatch] section also holds [dispatch.breaker] as its own sub-section
    body = sec.split("### ")[0]
    rows = {k: r for r in table_rows(body)[1:] for k in re.findall(r"`([^`]+)`", r[0])}
    keys = hs_config._SECTION_KEYS[name]
    assert set(rows) == set(keys), (name, sorted(set(rows) ^ set(keys)))
    for k, r in rows.items():
        want = hs_config.DEFAULTS[name].get(k)
        if k == "breaker":
            assert "table" in r[1]
        elif want is not None:
            assert norm(r[1]) == (float(want) if isinstance(want, (int, float)) and not isinstance(want, bool)
                                  else want), f"[{name}] {k}: docs say {r[1]!r}, code says {want!r}"


def test_config_page_breaker_section():
    sec = section(page("docs/CONFIGURATION.md"), "`[dispatch.breaker]`", 3)
    rows = {k: r for r in table_rows(sec)[1:] for k in re.findall(r"`([^`]+)`", r[0])}
    assert set(rows) == hs_config._BREAKER_KEYS
    for k, r in rows.items():
        assert norm(r[1]) == float(hs_config.DEFAULTS["dispatch"]["breaker"][k]), k


def test_config_page_spans_match_the_presets():
    sec = section(page("docs/CONFIGURATION.md"), "`[spans]`")
    got = {re.findall(r"`([^`]+)`", r[0])[0]: (float(r[1]), float(r[2])) for r in table_rows(sec)[1:]}
    want = {k: (float(v["quiet_s"]), float(v["max_s"])) for k, v in hs_config.DEFAULTS["spans"].items()}
    assert got == want


def parse_duration(s: str) -> float:
    s = s.strip()
    m = re.fullmatch(r"([\d.]+)\s*(s|min)?", s)
    assert m, s
    return float(m.group(1)) * (60 if m.group(2) == "min" else 1)


def test_readme_spans_match_the_presets():
    sec = section(page("README.md"), "Configuration")
    rows = [r for r in table_rows(sec) if r and re.fullmatch(r"`\w+`", r[0])]
    got = {r[0].strip("`"): (parse_duration(r[1]), parse_duration(r[2])) for r in rows}
    want = {k: (float(v["quiet_s"]), float(v["max_s"])) for k, v in hs_config.DEFAULTS["spans"].items()}
    assert got == want


def test_config_page_source_route_and_destination_keys():
    text = page("docs/CONFIGURATION.md")
    src = first_cell_keys(section(text, "`[sources.<name>]`"))
    assert hs_config.SOURCE_KEYS <= src, sorted(hs_config.SOURCE_KEYS - src)
    assert src <= hs_config.SOURCE_KEYS | set(template.config_keys) | {"env"}, sorted(src - hs_config.SOURCE_KEYS)
    rt = first_cell_keys(section(text, "`[[routes]]`"))
    assert rt == hs_config.ROUTE_KEYS, sorted(rt ^ hs_config.ROUTE_KEYS)
    dsec = section(text, "`[destinations.<name>]`")
    rows = {k: r for r in table_rows(dsec)[1:] for k in re.findall(r"`([^`]+)`", r[0])}
    every = set().union(*hs_config.DEST_KEYS.values())
    assert set(rows) == every, sorted(set(rows) ^ every)
    for t, keys in hs_config.DEST_KEYS.items():
        for k in keys:
            applies = rows[k][1]
            assert "all" in applies or t in applies, f"destinations: `{k}` applies to {t} but docs say {applies!r}"


def test_config_page_names_the_removed_keys_and_secret_forms():
    text = page("docs/CONFIGURATION.md")
    for k in ("limits.max_pending_batches", "limits.max_db_bytes", "limits.plugin_error_streak",
              "retention.archive_dir", "dispatch.poison_after", "dispatch.dead_after_s", "delay_s", "rate_per_min",
              "idempotency", "paused"):
        assert f"`{k}`" in text, k
    assert set(re.findall(r"^\| `(\{ [^`]+ \})`", section(text, "Secrets"), re.M)) == {
        '{ file = "/path" }', '{ env_file = "/path", key = "NAME" }', '{ env = "NAME" }'}


# ---- sources ------------------------------------------------------------------------------------------------------
def test_adding_a_source_lists_every_template_key_scheme_and_selector():
    text = page("docs/ADDING_A_SOURCE.md")
    ref = section(text, "A. A config template", 2)
    named = ticks(ref)
    for k in template.config_keys:
        assert k in named, f"template key `{k}` is not documented"
    for s in template.SCHEMES:
        assert s in named, f"scheme `{s}` is not documented"
    for sel in ("a.b.0.c", "header:Name", "literal:text", "body_sha256"):
        assert sel in named, sel
    # a documented scheme row lists the keys the code accepts for it
    rows = {r[0].strip("`"): r[2] for r in table_rows(ref) if len(r) == 3 and r[0].strip("`") in template.SCHEMES}
    for scheme, keys in template._SCHEME_KEYS.items():
        for k in keys:
            assert f"`{k}`" in rows[scheme], f"{scheme}: `{k}`"


def test_adding_a_source_lists_every_plugin_name_and_only_real_helpers():
    text = page("docs/ADDING_A_SOURCE.md")
    plugin = section(text, "B. A code plugin", 2)
    names = {re.match(r"`?(\w+)", c).group(1) for r in table_rows(plugin) for c in r[:1] if re.match(r"`?\w+", c)}
    for n in (*sourcekit.PLUGIN_FUNCS, "auth_headers", "name", "secrets", "keep_headers", "span", "expect_every_s",
              "sender_retries", "challenge_unauthenticated", "dedup_id", "config_keys"):
        assert n in names, f"plugin name `{n}` is not documented"
    for h in set(re.findall(r"\bv\.(\w+)\(", text)):
        assert h in hs_verify.__all__, f"`v.{h}` is not a hookspool.verify helper"
    for h in ("hmac_hex", "hmac_b64", "hmac_prefixed", "timestamped", "standard_webhooks", "static_token", "jwt_es256"):
        assert f"v.{h}(" in text, h


def test_adding_a_source_conformance_table_matches_the_kit():
    text = section(page("docs/ADDING_A_SOURCE.md"), "The conformance kit")
    rows = [r[0] for r in table_rows(text)[1:]]
    for case in sourcekit.CASE_IDS:
        words = case.replace("_", " ")
        hit = any(words in r.lower() or all(w in r.lower() for w in words.split()) for r in rows)
        alias = {"no length": "chunked", "handshake bad signature": "handshake with a bad signature",
                 "unauth handshake constant": "unauthenticated handshake"}.get(words)
        assert hit or (alias and any(alias in r.lower() for r in rows)), f"kit case {case} is not described"


# ---- toml blocks --------------------------------------------------------------------------------------------------
class _AnyKeys:
    """A stand-in plugin for a module the docs name but this checkout does not have: it accepts any source key."""

    def __init__(self):
        self.config_keys = ()


BAD = re.compile(r"unknown key|removed in v2|unknown span|unknown destination type|must be|not used by scheme|"
                 r"required: a selector|not a valid|not a dotted path|selector|expected \"<selector>")


def toml_blocks(path: str):
    for m in re.finditer(r"```toml\n(.*?)```", read(path), re.S):
        yield m.group(1)


TOML_PAGES = [p for p in FOLLOWED if list(toml_blocks(p))]


@pytest.mark.parametrize("path", TOML_PAGES, ids=[os.path.basename(p) for p in TOML_PAGES])
def test_toml_blocks_parse_and_pass_key_validation(path):
    for i, block in enumerate(toml_blocks(path)):
        doc = tomllib.loads(block)
        cfg = hs_config.merge(hs_config.DEFAULTS, doc)
        custom = {n for n, s in (doc.get("sources") or {}).items()
                  if isinstance(s, dict) and s.get("module") and not str(s["module"]).startswith("hookspool.")}

        def loader(name, module, custom=custom):
            if name in custom:
                raise ImportError(name)
            return hs_config.default_plugin_loader(name, module)

        rep = hs_config.check(cfg, load_plugin=loader, check_secrets=False)
        errs = [e for e in rep.errors if BAD.search(e) and not any(e.startswith(f"sources.{c}") for c in custom)]
        assert not errs, f"{os.path.basename(path)} toml block {i}: {errs}"


# ---- the examples run ----------------------------------------------------------------------------------------------
def python_blocks(path: str) -> list[str]:
    return re.findall(r"```python\n(.*?)```", read(path), re.S)


def run_pytest(tmp_path, *files, extra_path=(), conf: str = "") -> subprocess.CompletedProcess:
    (tmp_path / "docs_conftest.py").write_text(conf)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), *extra_path, ROOT, os.path.join(ROOT, "tests")])
    env.pop("HOOKSPOOL_DENYLIST", None)
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "docs_conftest",
                           "-p", "conftest", "--rootdir", str(tmp_path), "-c", os.devnull, *map(str, files)],
                          capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=180)


def test_worked_source_example_passes_the_conformance_kit(tmp_path):
    plugin, test = python_blocks(os.path.join(DOCS, "ADDING_A_SOURCE.md"))[:2]
    (tmp_path / "extra_sources").mkdir()
    (tmp_path / "extra_sources" / "acme.py").write_text(plugin)
    (tmp_path / "test_sources_acme.py").write_text(test)
    r = run_pytest(tmp_path, tmp_path / "test_sources_acme.py", conf=(
        "import hookspool.sources as s\n"
        f"s.__path__.append({str(tmp_path / 'extra_sources')!r})\n"))
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1500:]
    assert re.search(r"\d+ passed", r.stdout)


def test_worked_destination_example_passes_its_tests(tmp_path):
    blocks = python_blocks(os.path.join(DOCS, "ADDING_A_DESTINATION.md"))
    module = next(b for b in blocks if "class ChatDestination" in b)
    test = next(b for b in blocks if "def test_a_2xx_is_delivered" in b)
    (tmp_path / "mychat.py").write_text(module)
    (tmp_path / "test_dest_mychat.py").write_text(test)
    r = run_pytest(tmp_path, tmp_path / "test_dest_mychat.py")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-1500:]
    assert re.search(r"\d+ passed", r.stdout)


def test_the_documented_destination_example_names_the_documented_config():
    text = page("docs/ADDING_A_DESTINATION.md")
    block = next(b for b in toml_blocks(os.path.join(DOCS, "ADDING_A_DESTINATION.md")) if "mychat" in b)
    assert tomllib.loads(block)["destinations"]["chat"]["module"] == "mychat:ChatDestination"
    assert "mychat:ChatDestination" in text


def test_readme_quick_start_config_passes_check(tmp_path, monkeypatch, capsys):
    qs = section(page("README.md"), "Quick start")
    m = re.search(r'config\.toml" <<EOF\n(.*?)\nEOF', qs, re.S)
    assert m, "the quick start has no config heredoc"
    home = tmp_path / "demo"
    (home / "secrets").mkdir(parents=True)
    secret = home / "secrets" / "demo"
    secret.write_text("demo-secret")
    secret.chmod(0o600)
    script = home / "show_batch.sh"
    script.write_text("#!/bin/sh\ncat > /dev/null\n")
    script.chmod(0o755)
    cfg = home / "config.toml"
    cfg.write_text(m.group(1).replace("$HOOKSPOOL_HOME", str(home)))
    monkeypatch.setenv("HOOKSPOOL_HOME", str(home))
    monkeypatch.setenv("HOOKSPOOL_CONFIG", str(cfg))
    rc = cli.main(["check"])
    out = capsys.readouterr()
    assert rc == 0 and out.out.strip() == "ok", (out.out, out.err)
    assert out.err == "", out.err           # not even a warning in the first thing a reader runs


# ---- links, hostnames ---------------------------------------------------------------------------------------------
def slug(heading: str) -> str:
    s = re.sub(r"[`*_]", "", heading.strip()).lower()
    s = re.sub(r"[^a-z0-9 \-]", "", s)
    return s.replace(" ", "-")


def anchors(path: str) -> set[str]:
    text = re.sub(r"```.*?```", "", read(path), flags=re.S)
    seen: dict[str, int] = {}
    out = set()
    for h in re.findall(r"^#{1,6} (.+?)\s*$", text, re.M):
        s = slug(h)
        n = seen.get(s, 0)
        seen[s] = n + 1
        out.add(s if n == 0 else f"{s}-{n}")
    return out


LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")


@pytest.mark.parametrize("path", PAGES, ids=[os.path.relpath(p, ROOT) for p in PAGES])
def test_relative_links_and_anchors_resolve(path):
    text = re.sub(r"```.*?```", "", read(path), flags=re.S)
    for target in LINK.findall(text):
        if re.match(r"[a-z]+://|mailto:", target):
            continue
        rel, _, frag = target.partition("#")
        dest = os.path.normpath(os.path.join(os.path.dirname(path), rel)) if rel else path
        assert os.path.exists(dest), f"{os.path.relpath(path, ROOT)}: {target} -> {dest} does not exist"
        if frag and dest.endswith(".md"):
            assert frag in anchors(dest), f"{os.path.relpath(path, ROOT)}: {target}: no heading #{frag} in {dest}"


ALLOWED_HOSTS = re.compile(
    r"(^|\.)(example\.com|example\.test|example\.invalid|localhost)$|^127\.0\.0\.1$|"
    r"^(github\.com|docs\.github\.com|www\.standardwebhooks\.com)$")


@pytest.mark.parametrize("path", PAGES, ids=[os.path.relpath(p, ROOT) for p in PAGES])
def test_pages_carry_no_hostnames_addresses_or_deployment_terms(path):
    text = read(path)
    bad = []
    for ip in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text):
        if ip not in ("127.0.0.1", "0.0.0.0"):
            bad.append(ip)
    for host in re.findall(r"https?://([\w.-]+)", text):
        if not ALLOWED_HOSTS.search(host):
            bad.append(host)
    bad += [m.group(0) for m in deny_regex(local_deny_terms()).finditer(text)]
    if os.path.basename(path) != "ARCHITECTURE.md":
        bad += re.findall(r"/root/\S*", text)
    assert not bad, (os.path.relpath(path, ROOT), bad)


def test_pages_carry_no_real_looking_secrets():
    pat = re.compile(r"(ghp_|gho_|xox[bap]-|sk-[A-Za-z0-9]{20}|AKIA[0-9A-Z]{12}|-----BEGIN [A-Z ]*PRIVATE KEY)")
    for p in PAGES:
        assert not pat.search(read(p)), p


# ---- what the project promises ------------------------------------------------------------------------------------
def test_readme_has_the_promised_sections():
    r = page("README.md")
    for h in ("The problem", "What it guarantees", "What it does not do", "How it works", "Quick start", "Configuration",
              "Sources and destinations", "Operations", "FAQ", "How it compares", "Documentation", "License"):
        assert re.search(rf"^## {re.escape(h)}\s*$", r, re.M), h
    assert "```mermaid" in r and "flowchart" in r
    comp = section(r, "How it compares")
    head = table_rows(comp)[0]
    for name in ("Convoy", "Svix", "Hookdeck", "Inngest"):
        assert any(name in c for c in head), name
    # honest: the comparison says when to choose each of the others
    assert "Choose it when" in comp and "use one of the others" in " ".join(comp.split())
    guarantees = section(r, "What it guarantees")
    for g in ("Commit before acknowledge", "At-least-once", "Order per key", "Bounded batches"):
        assert g in guarantees, g
    assert "Exactly-once" in section(r, "What it does not do")


def test_readme_documentation_table_covers_every_page():
    r = page("README.md")
    for p in sorted(glob.glob(os.path.join(DOCS, "*.md"))):
        assert f"docs/{os.path.basename(p)}" in r, os.path.basename(p)
    for f in ("CONTRIBUTING.md", "CHANGELOG.md", "LICENSE", "deploy/README.md"):
        assert f in r, f


def test_contributing_calls_out_the_extension_model_before_the_setup_steps():
    c = page("CONTRIBUTING.md")
    assert c.index("## Adding a sender: the extension model") < c.index("## Setting up")
    model = " ".join(section(c, "Adding a sender: the extension model").split())
    for phrase in ("Config template first", "Code plugin when you need it", "conformance kit", "CI enforces it",
                   "no Python", "sender's signature documentation"):
        assert phrase.lower() in model.lower(), phrase
    assert model.index("Config template first") < model.index("Code plugin when you need it") \
        < model.index("conformance kit")
    for ref in ("docs/ADDING_A_SOURCE.md", "docs/ADDING_A_DESTINATION.md", "examples/sources/github.toml"):
        assert ref in c, ref


def test_changelog_has_an_unreleased_section_with_added():
    c = page("CHANGELOG.md")
    assert re.search(r"^## \[Unreleased\]\s*$", c, re.M) and re.search(r"^### Added\s*$", c, re.M)


# ---- packaging ----------------------------------------------------------------------------------------------------
def pyproject() -> dict:
    return tomllib.loads(page("pyproject.toml"))


def test_pyproject_is_complete():
    p = pyproject()
    proj = p["project"]
    assert proj["name"] == "hookspool" and proj["requires-python"] == ">=3.11"
    assert "version" in proj["dynamic"] and p["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "hookspool.__version__"}
    assert proj["license"] == "MIT" and os.path.exists(os.path.join(ROOT, "LICENSE"))
    assert proj["readme"] == "README.md" and proj["dependencies"] == []        # the core is standard library only
    assert {"plaid", "dev"} <= set(proj["optional-dependencies"])
    assert any(d.lower().startswith("pyjwt") for d in proj["optional-dependencies"]["plaid"])
    assert any(d.lower().startswith("cryptography") for d in proj["optional-dependencies"]["plaid"])
    target = proj["scripts"]["hookspool"]
    mod, _, func = target.partition(":")
    assert callable(getattr(__import__(mod, fromlist=[func]), func))
    assert "migrations/*.sql" in p["tool"]["setuptools"]["package-data"]["hookspool"]
    assert re.fullmatch(r"\d+\.\d+\.\d+(\.dev\d+)?", __import__("hookspool").__version__)
    for classifier in proj["classifiers"]:
        assert not classifier.startswith("License ::"), "PEP 639: the licence is the `license` field"
    ignored = page(".gitignore")
    for pat in ("build/", "dist/", "*.egg-info/"):
        assert pat in ignored, pat


def test_license_is_mit():
    lic = page("LICENSE")
    assert lic.startswith("MIT License") and "Permission is hereby granted, free of charge" in lic


def build_tool() -> list[str] | None:
    import importlib.util
    try:  # a stray ./build directory is a namespace package with no __main__; only the real tool counts
        if importlib.util.find_spec("build.__main__") is not None:
            return [sys.executable, "-m", "build", "--wheel", "--outdir"]
    except (ImportError, ValueError):
        pass
    uv = shutil.which("uv")
    return [uv, "build", "--wheel", "--out-dir"] if uv else None


def test_a_wheel_builds_and_carries_what_the_program_needs(tmp_path):
    tool = build_tool()
    if tool is None:
        pytest.skip("neither `python -m build` nor `uv` is available")
    src = tmp_path / "src"
    src.mkdir()
    for f in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy(os.path.join(ROOT, f), src / f)
    shutil.copytree(os.path.join(ROOT, "hookspool"), src / "hookspool", ignore=shutil.ignore_patterns("__pycache__"))
    out = tmp_path / "dist"
    r = subprocess.run([*tool, str(out), str(src)], capture_output=True, text=True, timeout=300)
    blob = r.stdout + r.stderr
    if r.returncode != 0 and re.search(r"(?i)failed to fetch|no matching distribution|could not find a version|"
                                       r"temporary failure in name resolution|name or service not known|"
                                       r"network is unreachable|connection (?:error|refused|reset)|"
                                       r"max retries exceeded|failed to download|offline", blob):
        pytest.skip("the build backend could not be fetched (offline): " + blob[-300:])
    assert r.returncode == 0, blob[-2000:]
    (wheel,) = glob.glob(str(out / "*.whl"))
    import zipfile
    names = zipfile.ZipFile(wheel).namelist()
    version = __import__("hookspool").__version__
    assert os.path.basename(wheel).startswith(f"hookspool-{version}-")
    assert "hookspool/migrations/0001_v2.sql" in names
    assert any(n.endswith("licenses/LICENSE") for n in names)
    entry = next(n for n in names if n.endswith("entry_points.txt"))
    assert "hookspool = hookspool.__main__:main" in zipfile.ZipFile(wheel).read(entry).decode()
    assert not any("/tests/" in n or n.startswith("tests/") or "deploy/local" in n for n in names)
    for pkg in ("hookspool/sources/template.py", "hookspool/destinations/hermes.py", "hookspool/__main__.py"):
        assert pkg in names, pkg
