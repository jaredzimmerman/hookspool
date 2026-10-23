"""CI tooling (T8c): ``.github/scripts/plugin_ci.py`` and the workflow files.

* discovery of the sources and templates a change touches (against a throwaway git repo)
* the conformance kit REJECTS a deliberately bad plugin (``tests/badkit``) and passes the shipped ones
* the core-has-no-per-source-code guard, the secret/hostname scan (positives and negatives)
* the workflows: ``pull_request`` only, read-only token, the Python matrix, no expression interpolated into a shell
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, ".github", "scripts", "plugin_ci.py")
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")

spec = importlib.util.spec_from_file_location("plugin_ci", SCRIPT)
P = importlib.util.module_from_spec(spec)
sys.modules["plugin_ci"] = P          # dataclasses look the module up by name
spec.loader.exec_module(P)


def write(root, rel, text=""):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(textwrap.dedent(text))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A tiny git repo laid out like hookspool: two code sources, two templates, the template engine."""
    root = str(tmp_path)
    for rel in ("hookspool/sources/__init__.py", "hookspool/sources/template.py", "hookspool/sources/alpha.py",
                "hookspool/sources/beta.py", "examples/sources/gamma.toml", "examples/sources/delta.toml",
                "tests/captures/gamma/event.http", "tests/captures/alpha/event.http",
                "tests/test_sources_alpha.py", "tests/test_sources_beta.py", "tests/sourcekit.py",
                "README.md", "hookspool/store.py"):
        write(root, rel, "x = 1\n")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.test", "PATH": os.environ["PATH"], "HOME": root}
    for cmd in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
        subprocess.run(["git", "-C", root, *cmd], check=True, env=env, capture_output=True)
    monkeypatch.setattr(P, "ROOT", root)
    return root, env


def commit(root, env, *rels):
    for rel in rels:
        write(root, rel, "x = 2\n")
    subprocess.run(["git", "-C", root, "add", "-A"], check=True, env=env, capture_output=True)
    subprocess.run(["git", "-C", root, "commit", "-q", "-m", "change"], check=True, env=env, capture_output=True)


# ---- discovery -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("touched,expected", [
    (["hookspool/sources/alpha.py"], ["code:alpha"]),
    (["examples/sources/gamma.toml"], ["template:gamma"]),
    (["tests/captures/gamma/event.http"], ["template:gamma"]),
    (["tests/captures/alpha/event.http"], ["code:alpha"]),
    (["tests/test_sources_beta.py"], ["code:beta"]),
    (["hookspool/sources/alpha.py", "examples/sources/delta.toml"], ["code:alpha", "template:delta"]),
    (["README.md", "hookspool/store.py"], []),                        # core and docs: no plugin kit
])
def test_discover_maps_changed_files_to_plugins(repo, touched, expected):
    root, env = repo
    assert P.discover(touched) == expected


@pytest.mark.parametrize("shared", ["tests/sourcekit.py", "hookspool/sources/template.py", "hookspool/verify.py"])
def test_a_change_to_shared_machinery_runs_every_plugin(repo, shared):
    assert P.discover([shared]) == ["code:alpha", "code:beta", "template:delta", "template:gamma"]


def test_changed_files_since_the_base_commit_includes_uncommitted_and_new_files(repo):
    root, env = repo
    base = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True,
                          env=env).stdout.strip()
    commit(root, env, "hookspool/sources/alpha.py")
    write(root, "examples/sources/new.toml", "a = 1\n")             # untracked
    write(root, "README.md", "changed\n")                           # modified, uncommitted
    write(root, ".ladder/notes.md", "ledger\n")                     # never scanned
    assert P.changed_files(base) == ["README.md", "examples/sources/new.toml", "hookspool/sources/alpha.py"]
    assert P.changed_files(None) == []


def test_deleted_files_are_not_checked(repo):
    root, env = repo
    base = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"], capture_output=True, text=True,
                          env=env).stdout.strip()
    os.remove(os.path.join(root, "hookspool/sources/beta.py"))
    subprocess.run(["git", "-C", root, "commit", "-qam", "rm"], check=True, env=env, capture_output=True)
    assert "hookspool/sources/beta.py" not in P.changed_files(base)


# ---- the conformance kit ---------------------------------------------------------------------------------
def test_the_kit_rejects_a_deliberately_bad_plugin():
    report = P.Report()
    P.run_kit("code:badsig", report, test_file="tests/badkit/kit_badsig.py")
    row, = report.rows
    assert not row.ok and not report.ok
    # the cases that catch it: accepting an unsigned or a tampered request
    assert "unsigned" in row.detail and "tampered" in row.detail
    assert "FAIL" in row.md()


def test_the_cli_exits_nonzero_for_the_bad_plugin_and_zero_for_a_good_one(tmp_path):
    env = dict(os.environ, GITHUB_STEP_SUMMARY=str(tmp_path / "summary.md"))
    bad = subprocess.run([sys.executable, SCRIPT, "kit", "code:badsig", "--test-file", "tests/badkit/kit_badsig.py"],
                         cwd=ROOT, env=env, capture_output=True, text=True)
    assert bad.returncode == 1, bad.stdout + bad.stderr
    good = subprocess.run([sys.executable, SCRIPT, "kit", "code:zoom", "template:github"],
                          cwd=ROOT, env=env, capture_output=True, text=True)
    assert good.returncode == 0, good.stdout + good.stderr
    summary = (tmp_path / "summary.md").read_text()
    assert "| Check | Subject | Result | Detail |" in summary          # the job summary table
    assert "`code:badsig` | **FAIL**" in summary and "`template:github` | pass" in summary


@pytest.mark.parametrize("spec", ["template:github", "template:kaneo", "template:trello", "code:zoom", "code:plaid",
                                  "code:gmail", "code:gcal"])
def test_every_shipped_source_passes_the_kit(spec):
    report = P.Report()
    P.run_kit(spec, report)
    assert report.ok, report.table()


def test_every_shipped_source_is_discoverable_and_has_a_kit_test():
    assert {"template:github", "template:kaneo", "template:trello"} <= {f"template:{t}" for t in P.all_templates()}
    assert {"zoom", "plaid", "gmail", "gcal"} <= set(P.all_code_sources())
    for name in P.all_code_sources():
        assert P.uses_kit(f"tests/test_sources_{name}.py"), name


def test_uses_kit_needs_real_code_not_a_comment(repo):
    root, _ = repo
    write(root, "tests/test_sources_alpha.py", """
        # parametrize over sourcekit.CASES, from sourcekit import CASES
        NOTE = "CASES and sourcekit"
    """)
    assert not P.uses_kit("tests/test_sources_alpha.py")
    write(root, "tests/test_sources_alpha.py", "import sourcekit\nX = sourcekit.CASES\n")
    assert P.uses_kit("tests/test_sources_alpha.py")
    write(root, "tests/test_sources_alpha.py", "from sourcekit import CASES\n")
    assert P.uses_kit("tests/test_sources_alpha.py")


def test_template_nodeids_do_not_confuse_a_name_with_a_longer_one(monkeypatch):
    out = "\n".join(f"tests/t.py::test_k[{c}-{n}.toml]" for c in ("signed", "no_length") for n in ("page", "status-page"))
    out += "\ntests/t.py::test_c[page/event.http]\ntests/t.py::test_c[status-page/event.http]"
    monkeypatch.setattr(P, "_pytest", lambda *a, **k: (0, out, 0.0))
    assert P.template_nodeids("page") == ["tests/t.py::test_k[signed-page.toml]", "tests/t.py::test_k[no_length-page.toml]",
                                          "tests/t.py::test_c[page/event.http]"]


def test_a_code_source_without_a_kit_test_fails(repo):
    report = P.Report()
    P.run_kit("code:alpha", report)                      # tests/test_sources_alpha.py exists but never touches the kit
    assert not report.ok and "sourcekit.CASES" in report.rows[0].detail
    os.remove(os.path.join(repo[0], "tests/test_sources_beta.py"))
    P.run_kit("code:beta", report)
    assert "is missing" in report.rows[1].detail


def test_a_template_without_a_capture_fails(repo):
    report = P.Report()
    P.run_kit("template:delta", report)
    assert not report.ok and "event.http" in report.rows[0].detail
    P.run_kit("template:nope", report)
    assert "does not exist" in report.rows[1].detail
    P.run_kit("weird:x", report)
    assert "spec must be" in report.rows[2].detail


# ---- core guard ------------------------------------------------------------------------------------------
def test_core_guard_passes_on_the_repo():
    report = P.Report()
    P.run_guard(report)
    assert report.ok, report.table()


def test_core_guard_flags_a_source_name_in_code_but_not_in_docs(repo):
    root, _ = repo
    write(root, "hookspool/core.py", '''
        """Mentions alpha in a docstring: fine."""
        # and beta in a comment: fine
        def f(source):
            if source == "alpha":              # per-source code in core: not fine
                return "X-Alpha-Event"
            return "alphabet"                  # a different word
        ''')
    hits = P.core_violations({"alpha", "beta", "gamma"}, files=["hookspool/core.py"], allow={})
    assert [(h[2], h[3]) for h in hits] == [("alpha", "alpha"), ("X-Alpha-Event", "alpha")]
    allowed = {("hookspool/core.py", "alpha"): "why", ("hookspool/core.py", "X-Alpha-Event"): "why"}
    assert P.core_violations({"alpha"}, files=["hookspool/core.py"], allow=allowed) == []


def test_core_files_exclude_the_source_plugins_but_guard_the_shared_engine():
    files = P.core_files()
    assert "hookspool/intake.py" in files and "hookspool/destinations/hermes.py" in files
    assert "hookspool/sources/template.py" in files and "hookspool/sources/__init__.py" in files
    for name in P.all_code_sources():
        assert f"hookspool/sources/{name}.py" not in files


# ---- secret / hostname scan ------------------------------------------------------------------------------
def kinds(text, rel="x.py", deny=None):
    return [k for _, k, _ in P.scan_text(rel, text, deny)]


@pytest.mark.parametrize("line,kind", [
    ("-----BEGIN " + "RSA PRIVATE KEY-----", "private key block"),
    ("t = 'ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8'", "GitHub token"),
    ("k = 'AKIA" + "ABCDEFGHIJKLMNOP'", "AWS access key id"),
    ("url = 'https://user:hunter22@db.example.test/x'", "credentials in a URL"),  # hookspool-scan: allow
    ("password = 'Zk3pQ8vN1xR7tY5uW2mB9cD4'", "assigned secret"),  # hookspool-scan: allow
    ("see https://hooks.mycompany-real.org/path", "hostname"),  # hookspool-scan: allow
    ("proxy = '8.8.4.4'", "IP address"),  # hookspool-scan: allow
    ("bot = '123456789:" + "AAH" + "x" * 32 + "'", "Telegram bot token"),
])
def test_scan_flags(line, kind):
    assert kind in kinds(line)


@pytest.mark.parametrize("line", [
    "https://hooks.example.test/hooks/x", "http://127.0.0.1:8080/", "http://localhost/x", "http://hookspool_intake",
    "https://api.github.com/x", "https://www.googleapis.com/calendar", "https://x.invalid/", "http://192.0.2.1:9/h",
    "http://10.0.0.1/", "ip = '127.0.0.1'", "version = '1.2.3.4.5'", "secret = 'test-template-secret-ABCDEFGHIJKLMNOP'",
    "token_file = '/etc/hookspool/token'", "x = '300.1.1.1'",
    "v = 'a' + '9.9.9.9'  # hookspool-scan: allow documented placeholder",
])
def test_scan_passes(line):
    assert kinds(line) == []


def test_scan_jwt_is_allowed_only_in_captures():
    jwt = "eyJhbGciOiJFUzI1NiJ9.eyJpYXQiOjE3fQ.c2lnbmF0dXJlLWJ5dGVz"  # hookspool-scan: allow
    assert "JWT" in kinds(f"Plaid-Verification: {jwt}", rel="README.md")
    assert kinds(f"Plaid-Verification: {jwt}", rel="tests/captures/plaid/event.http") == []


def test_scan_denylist_flags_without_echoing_the_term(tmp_path):
    f = tmp_path / "deny.txt"
    f.write_text("# comment\nmy-private-host\n")
    terms = P.local_deny_terms(env={"HOOKSPOOL_DENYLIST": "other-term,", "HOOKSPOOL_DENYLIST_FILE": str(f)})
    assert terms == ["my-private-host", "other-term"]
    found = P.scan_text("a.md", "deployed on My-Private-Host today", terms)
    assert found == [(1, "denylisted term", "<local denylist>")]
    assert P.local_deny_terms(env={"HOOKSPOOL_DENYLIST_FILE": str(tmp_path / "absent")}) == []


def test_scan_of_the_repository_is_clean():
    """Every tracked file (T9 removed collect.sh, the last one that carried deployment values)."""
    report = P.Report()
    P.run_scan(report, P.tracked_files())
    assert report.ok, report.table()


def test_scan_sees_a_bare_hostname_in_an_nginx_path():
    """The shape collect.sh had: a real host as a bare file name under sites-available."""
    line = "cp /etc/nginx/sites-available/" + "hooks.acme-prod" + ".net $O/nginx/\n"
    assert [k for _, k, _ in P.scan_text("collect.sh", line)] == ["hostname"]


@pytest.mark.parametrize("name,body,kind", [
    ("deploy/server.pem", "-----BEGIN " + "RSA PRIVATE KEY-----\nabc\n", "private key block"),
    ("deploy/prod.env", "TOKEN=ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8\n", "GitHub token"),
    ("deploy/id_ed25519", "-----BEGIN " + "OPENSSH PRIVATE KEY-----\n", "private key block"),
    ("deploy/site.crt", "see hooks.realcorp.com\n", "hostname"),            # hookspool-scan: allow
    ("Procfile", "web: serve --host hooks.realcorp.org\n", "hostname"),      # hookspool-scan: allow
])
def test_scan_reads_every_text_file_whatever_its_extension(repo, name, body, kind):
    root, _ = repo
    write(root, name, body)
    report = P.Report()
    P.run_scan(report, [name])
    assert not report.ok and kind in report.rows[0].detail, report.table()


def test_scan_skips_binaries_and_non_utf8(repo):
    root, _ = repo
    with open(os.path.join(root, "blob.bin"), "wb") as f:
        f.write(b"\0\1\2 hooks.realcorp.com \0")                          # hookspool-scan: allow
    with open(os.path.join(root, "latin.txt"), "wb") as f:
        f.write("caf\xe9 hooks.realcorp.com".encode("latin-1"))             # hookspool-scan: allow
    report = P.Report()
    P.run_scan(report, ["blob.bin", "latin.txt"])
    assert report.ok and report.rows[0].subject == "0 files"


@pytest.mark.parametrize("line", [
    "server_name hooks.realcorp.com;",  # hookspool-scan: allow
    "cp x /etc/nginx/sites-available/webhook.realcorp.com",  # hookspool-scan: allow
    "mail me at ops@realcorp.com",  # hookspool-scan: allow
    "HOST=hooks.realcorp.io",  # hookspool-scan: allow
])
def test_scan_flags_bare_hostnames_outside_urls(line):
    assert "hostname" in kinds(line, rel="deploy/nginx.conf")


@pytest.mark.parametrize("line", [
    "server_name hooks.example.com;", "server_name hooks.example.test;", "log.info('x')", "logging.INFO",
    "see api.github.com/repos", "run collect.sh and config.toml", "import os.path",
    "https://hooks.example.test/a", "ref: refs/heads/main.com-fix",
])
def test_scan_passes_bare_names_that_are_not_hosts(line):
    assert kinds(line, rel="deploy/nginx.conf") == []


def test_scan_python_files_ignore_attribute_lookalikes():
    assert kinds("x = self.io.app.dev.me", rel="hookspool/x.py") == []
    assert "hostname" in kinds("H = 'hooks.realcorp.com'", rel="hookspool/x.py")  # hookspool-scan: allow


def test_scan_all_skips_ladder_unless_asked(repo):
    root, env = repo
    write(root, ".ladder/RULES.md", "deployed at hooks.realcorp.com\n")       # hookspool-scan: allow
    subprocess.run(["git", "-C", root, "add", "-A"], check=True, env=env, capture_output=True)
    assert ".ladder/RULES.md" not in P.tracked_files()
    assert ".ladder/RULES.md" in P.tracked_files(include_ladder=True)
    report = P.Report()
    P.run_scan(report, P.tracked_files(include_ladder=True))
    assert not report.ok and report.rows[0].subject == ".ladder/RULES.md:1"


def test_scan_reports_findings_with_file_and_line(repo):
    root, _ = repo
    write(root, "docs.md", "line one\nhost: https://real-service.acme-prod.net/x\n")  # hookspool-scan: allow
    report = P.Report()
    P.run_scan(report, ["docs.md"])
    assert not report.ok
    assert report.rows[0].subject == "docs.md:2" and "acme-prod.net" in report.rows[0].detail  # hookspool-scan: allow


# ---- workflows -------------------------------------------------------------------------------------------
def workflow(name):
    with open(os.path.join(WORKFLOWS, name), encoding="utf-8") as f:
        return f.read()


@pytest.mark.parametrize("name", ["ci.yml", "plugins.yml", "bobbin.yml"])
def test_workflows_are_safe_for_public_pull_requests(name):
    text = workflow(name)
    assert "pull_request_target" not in text
    assert re.search(r"^on:\n(?:.*\n)*?  pull_request:", text, re.M)
    assert re.search(r"^permissions:\n  contents: read\n", text, re.M)
    assert not re.search(r"^\s+(?:contents|pull-requests|issues|id-token|actions): write", text, re.M)
    assert "persist-credentials: false" in text
    assert "${{ secrets." not in text
    assert "${{ vars." not in text            # step env values are printed in the (public) run log
    assert "DENYLIST" not in text.replace("HOOKSPOOL_DENYLIST or", "")    # the owner denylist is local-only
    for m in re.finditer(r"^(\s*)run:\s*(\|?)\s*(.*)$", text, re.M):         # nothing interpolated into a shell
        body = m.group(3)
        if m.group(2):
            indent = len(m.group(1)) + 2
            tail = text[m.end():].split("\n")[1:]
            body = "\n".join(ln for ln in _block(tail, indent))
        assert "${{" not in body, f"{name}: expression inside run: {body!r}"


def _block(lines, indent):
    for ln in lines:
        if ln.strip() and len(ln) - len(ln.lstrip()) < indent:
            break
        yield ln


def test_ci_matrix_and_commands():
    text = workflow("ci.yml")
    assert 'python: ["3.11", "3.12", "3.13"]' in text
    assert "ruff check ." in text and "python -m pytest -q" in text
    assert "requirements-dev.txt" in text


def test_plugins_workflow_runs_the_script_with_the_base_sha():
    text = workflow("plugins.yml")
    assert "fetch-depth: 0" in text
    assert "BASE_SHA: ${{ github.event.pull_request.base.sha }}" in text
    assert 'plugin_ci.py all --skip-bobbins --base "$BASE_SHA"' in text


def test_workflow_files_parse_as_yaml():
    yaml = pytest.importorskip("yaml")
    for name in ("ci.yml", "plugins.yml"):
        doc = yaml.safe_load(workflow(name))
        assert doc["permissions"] == {"contents": "read"}
        assert "pull_request" in (doc.get(True) or doc.get("on"))      # PyYAML reads the key `on` as True


def test_pull_request_template_asks_for_the_signature_docs_link():
    with open(os.path.join(ROOT, ".github", "pull_request_template.md"), encoding="utf-8") as f:
        text = f.read()
    assert "signature documentation (link)" in text
    assert "plugin_ci.py kit" in text
    assert kinds(text, rel=".github/pull_request_template.md") == []


# ---- bobbins ---------------------------------------------------------------------------------------------
import json  # noqa: E402
import shutil  # noqa: E402

import _miniyaml  # noqa: E402

FIX_BOBBINS = "tests/fixtures/bobbinkit"


@pytest.fixture
def brepo(repo):
    """The tiny repo plus two built-in bobbins, a contributed one elsewhere and a kit fixture."""
    root, env = repo
    for rel in ("hookspool/bobbins/__init__.py", "hookspool/bobbins/alpha/bobbin.toml", "hookspool/bobbins/alpha/source.py",
                "hookspool/bobbins/web-hook/bobbin.toml", "hookspool/providers/acme.py", "contrib/acme/bobbin.toml",
                "contrib/acme/captures/event.http", "tests/fixtures/bobbinkit/broken/bobbin.toml",
                "tests/bobbins/test_web_hook.py", "hookspool/net.py"):
        write(root, rel, "x = 1\n")
    commit(root, env)
    return root, env


@pytest.mark.parametrize("touched,expected", [
    (["hookspool/bobbins/alpha/source.py"], ["hookspool/bobbins/alpha"]),
    (["contrib/acme/captures/event.http"], ["contrib/acme"]),               # a bobbin added anywhere
    (["tests/bobbins/test_web_hook.py"], ["hookspool/bobbins/web-hook"]),
    (["tests/fixtures/bobbinkit/broken/bobbin.toml"], []),                  # the kit's own fixtures: never
    (["README.md", "hookspool/store.py"], []),
    (["hookspool/net.py"], ["hookspool/bobbins/alpha", "hookspool/bobbins/web-hook"]),
    (["hookspool/testing/bobbinkit.py", "contrib/acme/bobbin.toml"],
     ["contrib/acme", "hookspool/bobbins/alpha", "hookspool/bobbins/web-hook"]),
])
def test_discover_bobbins(brepo, touched, expected):
    assert P.discover_bobbins(touched) == expected


def test_core_guard_skips_bobbins_and_providers_but_knows_their_names(brepo):
    files = P.core_files()
    assert "hookspool/bobbins/__init__.py" in files and "hookspool/net.py" in files
    assert not any(f.startswith(("hookspool/bobbins/alpha/", "hookspool/providers/")) for f in files)
    assert {"alpha", "web-hook"} <= P.source_names()
    assert "webhook" in P.GENERIC_BOBBINS


def test_run_bobbin_writes_the_scorecards(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    report = P.Report()
    doc = P.run_bobbin(f"{FIX_BOBBINS}/kitcode", report, report_dir=str(tmp_path / "out"))
    row, = report.rows
    assert row.ok and row.check == "bobbin" and row.detail.startswith("bobbin check kitcode: PASS")
    assert doc["ok"] and json.loads((tmp_path / "out" / "kitcode.json").read_text())["schema"] == "hookspool.bobbin-check/1"
    assert (tmp_path / "out" / "kitcode.md").read_text().startswith("### Bobbin `kitcode`: PASS")
    report.emit("Bobbin scorecards")
    summary = (tmp_path / "summary.md").read_text()
    assert "| bobbin | `tests/fixtures/bobbinkit/kitcode` | pass |" in summary and "### Bobbin `kitcode`" in summary


def test_the_bobbins_command_fails_for_a_failing_bobbin(tmp_path):
    bad = tmp_path / "kitcode"
    shutil.copytree(os.path.join(ROOT, FIX_BOBBINS, "kitcode"), bad)
    (bad / "README.md").write_text("# kitcode\n\nTODO\n")
    out = tmp_path / "reports"
    env = dict(os.environ, GITHUB_STEP_SUMMARY=str(tmp_path / "s.md"))
    p = subprocess.run([sys.executable, SCRIPT, "bobbins", str(bad), os.path.join(FIX_BOBBINS, "kittpl"),
                        "--report-dir", str(out), "--pr", "42"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert p.returncode == 1, p.stdout + p.stderr
    assert json.loads((out / "meta.json").read_text()) == {"pr": 42, "bobbins": ["kitcode", "kittpl"]}
    assert json.loads((out / "kitcode.json").read_text())["ok"] is False
    assert json.loads((out / "kittpl.json").read_text())["ok"] is True
    summary = (tmp_path / "s.md").read_text()
    assert "**FAIL**" in summary and "1 of 9 required checks fail (todos)" in summary


def test_the_bobbins_command_scores_two_bobbins_bobbin_only_with_reports_in_the_checkout(tmp_path):
    """Round-2 regression: with --report-dir inside the checkout, the first bobbin's scorecard files used to be
    untracked files by the time the second bobbin's scope check ran, so every bobbin after the first was scored
    touches-core. The reports are written after the last check; both cards must be bobbin-only."""
    root = tmp_path / "repo"
    dirs = []
    for name in ("kitcode", "kittpl"):
        d = root / "bobbins" / name
        shutil.copytree(os.path.join(ROOT, FIX_BOBBINS, name), d)
        dirs.append(str(d))
    (root / ".gitignore").write_text("__pycache__/\n")        # bobbin-reports/ deliberately not ignored
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.test", "PATH": os.environ["PATH"], "HOME": str(root)}
    for cmd in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "base"]):
        subprocess.run(["git", "-C", str(root), *cmd], check=True, env=env, capture_output=True)
    out = root / "bobbin-reports"      # inside the checkout, as the reviewer's probe ran it
    run_env = dict(os.environ, GITHUB_STEP_SUMMARY=str(tmp_path / "s.md"))
    p = subprocess.run([sys.executable, SCRIPT, "bobbins", *dirs, "--base", "HEAD", "--report-dir", str(out),
                        "--pr", "7"], cwd=ROOT, env=run_env, capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr
    for name in ("kitcode", "kittpl"):
        doc = json.loads((out / f"{name}.json").read_text())
        scope = next(c for c in doc["checks"] if c["name"] == "scope")
        assert doc["ok"] and doc["scope"] == "bobbin-only", (name, scope)
        assert scope["result"] == "pass" and scope["findings"] == [], (name, scope)
    assert json.loads((out / "meta.json").read_text()) == {"pr": 7, "bobbins": ["kitcode", "kittpl"]}


def test_the_bobbins_command_with_nothing_changed_writes_meta(tmp_path, repo):
    out = tmp_path / "reports"
    assert P.main(["bobbins", "--base", "HEAD", "--report-dir", str(out)]) == 0
    assert json.loads((out / "meta.json").read_text()) == {"pr": None, "bobbins": []}


def _wf(name):
    return _miniyaml.load(workflow(name))


def test_bobbin_workflow_jobs_and_permissions():
    doc = _wf("bobbin.yml")
    assert doc["on"] == {"pull_request": None}
    assert doc["permissions"] == {"contents": "read"}
    assert list(doc["jobs"]) == ["bobbin-check"]
    job = doc["jobs"]["bobbin-check"]
    assert job["permissions"] == {"contents": "read"}, "the PR job has a read-only token"
    steps = job["steps"]
    co = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@"))
    assert co["with"] == {"fetch-depth": 0, "persist-credentials": False}
    run = next(s for s in steps if "plugin_ci.py bobbins" in str(s.get("run", "")))
    assert run["run"] == ('python .github/scripts/plugin_ci.py bobbins --base "$BASE_SHA" --pr "$PR_NUMBER"'
                          ' --report-dir "$RUNNER_TEMP/bobbin-reports"'), \
        "the reports live outside the checkout: inside it they are untracked files the scope check would count"
    assert run["env"] == {"BASE_SHA": "${{ github.event.pull_request.base.sha }}",
                          "PR_NUMBER": "${{ github.event.pull_request.number }}"}
    up = next(s for s in steps if str(s.get("uses", "")).startswith("actions/upload-artifact@"))
    assert up["if"] == "always()" and up["with"]["name"] == "bobbin-reports"
    assert up["with"]["path"] == "${{ runner.temp }}/bobbin-reports/"


def test_label_workflow_runs_only_trusted_code_with_the_least_write_access():
    text = workflow("bobbin-label.yml")
    doc = _wf("bobbin-label.yml")
    assert doc["on"] == {"workflow_run": {"workflows": ["bobbin"], "types": ["completed"]}}
    assert "pull_request_target" not in text and "${{ secrets." not in text
    assert doc["permissions"] == {"contents": "read"}
    assert list(doc["jobs"]) == ["label"]
    job = doc["jobs"]["label"]
    assert job["if"] == "github.event.workflow_run.event == 'pull_request'"
    assert job["permissions"] == {"actions": "read", "contents": "read", "issues": "write", "pull-requests": "write"}
    steps = job["steps"]
    co = steps[0]
    assert str(co["uses"]).startswith("actions/checkout@") and co["with"] == {"persist-credentials": False}, \
        "the default branch only: no ref, so never the pull request's head"
    dl = next(s for s in steps if str(s.get("uses", "")).startswith("actions/download-artifact@"))
    assert dl["with"]["run-id"] == "${{ github.event.workflow_run.id }}" and dl["with"]["name"] == "bobbin-reports"
    runs = [s for s in steps if "run" in s]
    assert len(runs) == 1, "one step runs code: the default branch's label script, nothing installed"
    assert runs[0]["run"] == ('python3 .github/scripts/bobbin_label.py --reports bobbin-reports --repo "$REPO"'
                              ' --head-sha "$HEAD_SHA"')
    assert "${{" not in runs[0]["run"]
    assert runs[0]["env"]["HEAD_SHA"] == "${{ github.event.workflow_run.head_sha }}"


@pytest.mark.parametrize("name", ["ci.yml", "plugins.yml", "bobbin.yml", "bobbin-label.yml"])
def test_workflows_parse_with_the_reader(name):
    doc = _wf(name)
    assert doc["name"] and doc["jobs"] and "permissions" in doc


@pytest.mark.parametrize("name", ["ci.yml", "plugins.yml", "bobbin.yml", "bobbin-label.yml"])
def test_actionlint(name):
    exe = shutil.which("actionlint")
    if exe is None:
        pytest.skip("actionlint is not installed")
    p = subprocess.run([exe, os.path.join(WORKFLOWS, name)], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr


def test_miniyaml_refuses_what_it_cannot_read():
    with pytest.raises(ValueError):
        _miniyaml.load("a: {b: 1}\n")
    with pytest.raises(ValueError):
        _miniyaml.load("a: 1\na: 2\n")
    assert _miniyaml.load("a:\n  - x\n  - k: v\n    j: [1, 2]\nb: |\n  one\n  two\n") == \
        {"a": ["x", {"k": "v", "j": [1, 2]}], "b": "one\ntwo\n"}


def test_pull_request_template_has_the_bobbin_section():
    with open(os.path.join(ROOT, ".github", "pull_request_template.md"), encoding="utf-8") as f:
        text = f.read()
    sec = text[text.index("## New or changed bobbin"):]
    assert "hookspool bobbin check hookspool/bobbins/<name> --base origin/main" in sec
    boxes = re.findall(r"^- \[ \] (.+)$", sec.split("\n## ")[0], re.M)
    assert len(boxes) == 3, boxes
    assert "provider's documentation" in boxes[0] and "real delivery" in boxes[1] and "maintain" in boxes[2]
