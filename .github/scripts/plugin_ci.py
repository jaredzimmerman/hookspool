#!/usr/bin/env python3
"""Checks that .github/workflows/plugins.yml and bobbin.yml run on a pull request (stdlib and this
repository's own ``hookspool`` package only).

Subcommands (each prints a result table and exits 1 when any row failed):

  discover   which source plugins / template configs the change touches
  kit        run the shared conformance kit (hookspool/testing/sourcekit.py) on each of them
  bobbins    run ``hookspool bobbin check`` on every changed bobbin (or the directories given): the
             scorecards go to the job summary, and with --report-dir to <dir>/<name>.json and .md
  guard      the core-has-no-per-source-code guard
  scan       secret / hostname / IP scan of the changed files
  all        discover + kit + bobbins + guard + scan, and the job summary table

Everything runs locally the same way as in CI::

    python .github/scripts/plugin_ci.py all --base origin/main
    python .github/scripts/plugin_ci.py kit template:github.repo code:zoom
    python .github/scripts/plugin_ci.py bobbins --base origin/main
    python .github/scripts/plugin_ci.py scan --all                     # every tracked and untracked file

The table is also appended to ``$GITHUB_STEP_SUMMARY`` when that is set.

What a "plugin" is
  ``template:<name>``  ``hookspool/bobbins/<vendor>/<product>/bobbin.toml`` with a fixed ``[source.template]``, named
                       by the bobbin's ``<vendor>.<product>`` name (captures in
                       its ``captures/``); run through ``tests/test_sources_template.py`` (kit cases, captures, the
                       capture-coverage check), selected by test id. This is the repository's own regression run;
                       a contributor's command is ``hookspool bobbin check`` (the ``bobbins`` subcommand).
  ``code:<name>``      ``hookspool/sources/<name>.py``, run through ``tests/test_sources_<name>.py``, which
                       must parametrize over ``sourcekit.CASES`` (no kit, no merge).

What a changed bobbin is
  Any directory with a ``bobbin.toml`` that holds a changed file (``hookspool/bobbins/<vendor>/<product>/`` or one
  added anywhere else in the pull request, except the kit's own fixtures under ``tests/fixtures/``), the bobbin whose
  ``tests/bobbins/test_<vendor>_<product>.py`` changed, and every built-in bobbin when the machinery they run on
  changed.
  Each gets exactly the check a contributor runs: ``hookspool bobbin check <dir>``.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:                     # this repository's own package: the shared scan patterns, the kit
    sys.path.insert(0, ROOT)
from hookspool.testing import scan as _scan  # noqa: E402

SOURCES_DIR = "hookspool/sources"
EXAMPLES_DIR = "examples/sources"
BOBBINS_DIR = "hookspool/bobbins"
TEMPLATE_TESTS = "tests/test_sources_template.py"
# Shared machinery: a change to any of these can break every source, so the kit runs on all of them.
SHARED = {"tests/sourcekit.py", "hookspool/testing/sourcekit.py", "tests/conftest.py", "hookspool/plugin.py", "hookspool/verify.py",
          "hookspool/intake.py", "hookspool/sources/template.py", TEMPLATE_TESTS}
NOT_A_SOURCE = {"__init__", "template"}
# Never scanned: git's own directory. A file git ignores (``.gitignore``) is never listed in the first place.
SKIP_TREES = (".git/",)
# Captures are requests signed with test keys; a capture from a JWT-signing sender carries a JWT by design.
JWT_OK_UNDER = _scan.JWT_OK_UNDER
CORE_ALLOWLIST = os.path.join(".github", "core-allowlist.txt")
BOBBINS_DIR = "hookspool/bobbins"
PROVIDERS_DIR = "hookspool/providers"        # helpers shared by the bobbins of one provider (14.3.7): per-vendor
BOBBIN_MANIFEST = "bobbin.toml"
BOBBIN_TESTS = re.compile(r"tests/bobbins/test_([a-z0-9_]+)\.py")     # test_<vendor>_<product>.py, '.' and '-' as '_'
BOBBIN_NAME = re.compile(r"^[a-z][a-z0-9-]{0,39}\.[a-z][a-z0-9-]{0,39}$")
# The machinery every bobbin runs on: a change to it re-checks every built-in bobbin.
BOBBIN_SHARED = {"hookspool/bobbin_loader.py", "hookspool/processor.py", "hookspool/net.py", "hookspool/plugin.py",
                 "hookspool/verify.py", "hookspool/intake.py", "hookspool/config.py", "hookspool/sources/template.py"}
BOBBIN_SHARED_TREES = ("hookspool/testing/", PROVIDERS_DIR + "/")
NOT_A_BOBBIN_UNDER = ("tests/fixtures/",)   # the kit's own fixtures, some broken on purpose
# The namespace of the generic bobbins shipped with core (hookspool.webhook): a word core uses everywhere.
GENERIC_VENDORS = {"hookspool"}


# ---- results ---------------------------------------------------------------------------------------------
@dataclass
class Row:
    check: str
    subject: str
    ok: bool
    detail: str = ""

    def md(self) -> str:
        detail = self.detail.replace("|", "\\|").replace("\n", " ")
        return f"| {self.check} | `{self.subject}` | {'pass' if self.ok else '**FAIL**'} | {detail} |"


@dataclass
class Report:
    rows: list[Row] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)       # sections after the table (the bobbin scorecards)

    def add(self, check: str, subject: str, ok: bool, detail: str = "") -> Row:
        row = Row(check, subject, ok, detail)
        self.rows.append(row)
        return row

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.rows)

    def table(self) -> str:
        head = "| Check | Subject | Result | Detail |\n|---|---|---|---|\n"
        return head + "\n".join(r.md() for r in self.rows) + "\n"

    def emit(self, title: str) -> None:
        text = f"### {title}\n\n{self.table()}" + "".join("\n" + e for e in self.extra)
        print(text)
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if path:
            with open(path, "a", encoding="utf-8") as f:
                f.write(text + "\n")


def _git(*args: str) -> str:
    out = subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"git {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout


# ---- discovery -------------------------------------------------------------------------------------------
def _is_template_bobbin(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return isinstance(tomllib.load(f).get("source", {}).get("template"), dict)
    except (OSError, tomllib.TOMLDecodeError):
        return False


def _is_pointer(path: str) -> bool:
    """An examples file that only says where its template moved (comments, no table)."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f) == {}
    except (OSError, tomllib.TOMLDecodeError):
        return False


def builtin_bobbins() -> dict[str, str]:
    """``<vendor>.<product>`` -> the built-in bobbin's directory (relative), for ``hookspool/bobbins/<v>/<p>/``."""
    out: dict[str, str] = {}
    d = os.path.join(ROOT, BOBBINS_DIR)
    if not os.path.isdir(d):
        return out
    for v in sorted(os.listdir(d)):
        vdir = os.path.join(d, v)
        if v.startswith((".", "_")) or not os.path.isdir(vdir):
            continue
        for prod in sorted(os.listdir(vdir)):
            if os.path.isfile(os.path.join(vdir, prod, BOBBIN_MANIFEST)):
                rel = f"{BOBBINS_DIR}/{v}/{prod}"
                out[bobbin_name(rel)] = rel
    return out


def bobbin_name(rel_dir: str) -> str:
    """A bobbin directory's name: its manifest's ``name`` when that is a valid ``<vendor>.<product>``, else the
    directory's last two components joined by a dot."""
    try:
        with open(os.path.join(ROOT, rel_dir, BOBBIN_MANIFEST), "rb") as f:
            name = tomllib.load(f).get("name")
        if isinstance(name, str) and BOBBIN_NAME.match(name):
            return name
    except (OSError, tomllib.TOMLDecodeError):
        pass
    parts = rel_dir.rstrip("/").split("/")
    return ".".join(parts[-2:])


def all_templates() -> list[str]:
    names = {n for n, rel in builtin_bobbins().items()
             if _is_template_bobbin(os.path.join(ROOT, rel, BOBBIN_MANIFEST))}
    d = os.path.join(ROOT, EXAMPLES_DIR)
    if os.path.isdir(d):
        names |= {f[:-5] for f in os.listdir(d) if f.endswith(".toml") and not _is_pointer(os.path.join(d, f))}
    return sorted(names)


def template_manifest(name: str) -> str | None:
    """The file that defines a template: its bobbin manifest, else the legacy examples table."""
    own = builtin_bobbins().get(name)
    for rel in ([f"{own}/{BOBBIN_MANIFEST}"] if own else []) + [f"{EXAMPLES_DIR}/{name}.toml"]:
        if os.path.exists(os.path.join(ROOT, rel)):
            return rel
    return None


def capture_dir(name: str) -> str:
    own = builtin_bobbins().get(name)
    return f"{own}/captures" if own and os.path.isdir(os.path.join(ROOT, own, "captures")) else f"tests/captures/{name}"


def all_code_sources() -> list[str]:
    d = os.path.join(ROOT, SOURCES_DIR)
    return sorted(f[:-3] for f in os.listdir(d)
                  if f.endswith(".py") and f[:-3] not in NOT_A_SOURCE) if os.path.isdir(d) else []


def changed_files(base: str | None) -> list[str]:
    """Files added, copied, modified or renamed between ``base`` and HEAD (three dots: since the merge
    base), plus anything uncommitted when running locally. Deleted files are not checked."""
    if not base:
        return []
    names = set(_paths(_git("diff", "-z", "--name-only", "--diff-filter=ACMR", f"{base}...HEAD")))
    names |= set(_paths(_git("diff", "-z", "--name-only", "--diff-filter=ACMR", "HEAD")))
    names |= set(_paths(_git("ls-files", "-z", "--others", "--exclude-standard")))
    return sorted(n for n in names if not n.startswith(SKIP_TREES))


def _paths(out: str) -> list[str]:
    """The paths of a ``git ... -z`` listing: NUL separated and never quoted, so a name with a space, a newline or a
    non-ASCII character comes back exactly as it is on disk."""
    return [n for n in out.split("\0") if n]


def discover(files: list[str]) -> list[str]:
    """``kind:name`` for every plugin the changed ``files`` touch."""
    templates, codes = set(all_templates()), set(all_code_sources())
    found: set[str] = set()
    if any(f in SHARED for f in files):
        return sorted([f"template:{t}" for t in templates] + [f"code:{c}" for c in codes])

    def name_hit(name: str) -> None:
        if name in templates:
            found.add(f"template:{name}")
        if name in codes:
            found.add(f"code:{name}")

    for f in files:
        m = re.fullmatch(rf"{SOURCES_DIR}/([A-Za-z0-9_]+)\.py", f)
        if m and m.group(1) not in NOT_A_SOURCE:
            found.add(f"code:{m.group(1)}")
        m = re.fullmatch(rf"{EXAMPLES_DIR}/([A-Za-z0-9_-]+)\.toml", f)
        if m:
            found.add(f"template:{m.group(1)}")
        m = re.fullmatch(r"tests/captures/([A-Za-z0-9_-]+)/.+", f)
        if m:
            name_hit(m.group(1))
        m = re.fullmatch(rf"({BOBBINS_DIR}/[a-z][a-z0-9_-]*/[a-z][a-z0-9_-]*)/.+", f)
        if m and os.path.isfile(os.path.join(ROOT, m.group(1), BOBBIN_MANIFEST)):
            name_hit(bobbin_name(m.group(1)))
        m = re.fullmatch(r"tests/test_sources_([A-Za-z0-9_]+)\.py", f)
        if m and m.group(1) != "template":
            name_hit(m.group(1))
    return sorted(found)


# ---- bobbins ----------------------------------------------------------------------------------------------
def all_bobbins() -> list[str]:
    """The built-in bobbin directories (``hookspool/bobbins/<vendor>/<product>``), relative to the repository."""
    return sorted(builtin_bobbins().values())


def test_key(name: str) -> str:
    """The ``<key>`` of a bobbin's own test file ``tests/bobbins/test_<key>.py``: the name with ``.`` and ``-`` as ``_``."""
    return re.sub(r"[.-]", "_", name)


def bobbin_of(rel: str) -> str | None:
    """The bobbin directory (relative) that holds the file ``rel``, or None."""
    if rel.startswith(NOT_A_BOBBIN_UNDER):
        return None
    d = os.path.dirname(rel)
    while d:
        if os.path.isfile(os.path.join(ROOT, d, BOBBIN_MANIFEST)):
            return d
        d = os.path.dirname(d)
    return None


def discover_bobbins(files: list[str]) -> list[str]:
    """The bobbin directories a change touches (see the module docstring)."""
    found: set[str] = set()
    builtin = all_bobbins()
    if any(f in BOBBIN_SHARED or f.startswith(BOBBIN_SHARED_TREES) for f in files):
        found |= set(builtin)
    by_module = {test_key(bobbin_name(b)): b for b in builtin}
    for f in files:
        m = BOBBIN_TESTS.fullmatch(f)
        if m and m.group(1) in by_module:
            found.add(by_module[m.group(1)])
        b = bobbin_of(f)
        if b:
            found.add(b)
    return sorted(found)


def run_bobbin(rel_dir: str, report: Report, *, base: str | None = None, strict: bool = False,
               report_dir: str | None = None, pending: list[tuple[str, dict, str]] | None = None,
               online: bool = False) -> dict | None:
    """``hookspool bobbin check <dir> --report json`` (the contributor's command, run the same way), one result
    row, the Markdown scorecard in the job summary and, with ``report_dir``, ``<name>.json`` / ``<name>.md``.

    With ``pending`` the files are not written here but queued for ``write_reports``: a report written into
    the checkout between two checks would be an untracked file, and the next bobbin's ``scope`` check would
    list it as a change outside the bobbin (``touches-core``)."""
    from hookspool.testing import bobbinkit
    argv = [sys.executable, "-m", "hookspool", "bobbin", "check", rel_dir, "--report", "json"]
    if base:
        argv += ["--base", base]
    if strict:
        argv.append("--strict")
    if online:                      # the advisory provider-links row fetches the [provider] pages
        argv.append("--online")
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    t0 = time.monotonic()
    p = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True)
    secs = time.monotonic() - t0
    try:
        doc = json.loads(p.stdout)
        if not isinstance(doc, dict) or doc.get("schema") != bobbinkit.SCHEMA:
            raise ValueError("not a scorecard")
    except ValueError:
        tail = " | ".join((p.stderr or p.stdout).strip().splitlines()[-2:])[:300]
        report.add("bobbin", rel_dir, False, f"`hookspool bobbin check` exited {p.returncode} without a scorecard: {tail}")
        return None
    md = bobbinkit.render_md(doc)
    report.add("bobbin", rel_dir, bool(doc["ok"]) and p.returncode == 0, f"{doc['summary']} ({secs:.1f}s)")
    report.extra.append(md)
    if report_dir:
        name = re.sub(r"[^a-z0-9_-]", "_", str(doc.get("bobbin") or os.path.basename(rel_dir)))
        if pending is not None:
            pending.append((name, doc, md))
        else:
            write_reports(report_dir, [(name, doc, md)])
    return doc


def write_reports(report_dir: str, cards: list[tuple[str, dict, str]]) -> None:
    """``<name>.json`` and ``<name>.md`` for every queued scorecard, written once every bobbin is checked."""
    os.makedirs(report_dir, exist_ok=True)
    for name, doc, md in cards:
        with open(os.path.join(report_dir, f"{name}.json"), "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1, sort_keys=True)
        with open(os.path.join(report_dir, f"{name}.md"), "w", encoding="utf-8") as f:
            f.write(md)


def write_meta(report_dir: str, pr: str | None, names: list[str]) -> None:
    """``meta.json`` for the labelling job: the pull request number (which that job verifies against the
    run's head commit before trusting it) and the bobbins checked."""
    os.makedirs(report_dir, exist_ok=True)
    num = int(pr) if pr and pr.isdigit() else None
    with open(os.path.join(report_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"pr": num, "bobbins": names}, f, indent=1)


# ---- the conformance kit ---------------------------------------------------------------------------------
def _pytest(args: list[str], quiet: str = "-q") -> tuple[int, str, float]:
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, "-m", "pytest", quiet, "-p", "no:cacheprovider", "--no-header", *args],
                       cwd=ROOT, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr, time.monotonic() - t0


def _summary_line(out: str) -> str:
    lines = [ln for ln in out.strip().splitlines() if re.search(r"\b(passed|failed|error|no tests ran)\b", ln)]
    return lines[-1].strip(" =") if lines else "no pytest summary"


def _failed_cases(out: str) -> list[str]:
    ids = re.findall(r"^FAILED \S+?::\S+?\[(.+?)\]", out, flags=re.M)
    return ids


def uses_kit(path: str) -> bool:
    """A source's test module must really use the shared kit's ``CASES``: an ``sourcekit.CASES`` attribute
    or ``from sourcekit import CASES`` in code (a comment or string that mentions them does not count)."""
    try:
        with open(os.path.join(ROOT, path), encoding="utf-8") as f:
            tree = ast.parse(f.read(), path)
    except (OSError, SyntaxError, UnicodeDecodeError):
        return False
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr == "CASES"
                and isinstance(node.value, ast.Name) and node.value.id == "sourcekit"):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == "sourcekit" \
                and any(a.name == "CASES" for a in node.names):
            return True
    return False


def template_nodeids(name: str) -> list[str]:
    code, out, _ = _pytest(["--collect-only", TEMPLATE_TESTS], quiet="-q")
    if code != 0:
        return []
    ids = [ln.strip() for ln in out.splitlines() if "::" in ln and "[" in ln]
    # ids look like ``test_x[<case>-<name>]`` (or ``<name>.toml``) or ``test_y[<name>/event.http]``; a case id has no hyphen,
    # so a template ``page`` does not select the tests of ``status-page``.
    pat = re.compile(rf"\[(?:\w+-)?{re.escape(name)}(?:\.toml)?\]$|\[{re.escape(name)}/")
    return [i for i in ids if pat.search(i)]


def run_kit(spec: str, report: Report, test_file: str | None = None) -> None:
    kind, _, name = spec.partition(":")
    if kind == "template":
        if template_manifest(name) is None:
            report.add("kit", spec, False, f"{BOBBINS_DIR}/{name}/bobbin.toml does not exist")
            return
        event = f"{capture_dir(name)}/event.http"
        if not os.path.exists(os.path.join(ROOT, event)):
            report.add("kit", spec, False, f"{event} is required (a request signed with the test secret)")
            return
        ids = template_nodeids(name)
        if not ids:
            report.add("kit", spec, False, f"no tests collected for {name} in {TEMPLATE_TESTS}")
            return
        code, out, secs = _pytest(ids)
    elif kind == "code":
        test_file = test_file or f"tests/test_sources_{name}.py"
        if not os.path.exists(os.path.join(ROOT, test_file)):
            report.add("kit", spec, False, f"{test_file} is missing: a source plugin needs a kit test "
                                           f"(see tests/sourcekit.py)")
            return
        if not uses_kit(test_file):
            report.add("kit", spec, False, f"{test_file} does not parametrize over sourcekit.CASES")
            return
        code, out, secs = _pytest([test_file])
    else:
        report.add("kit", spec, False, "spec must be template:<name> or code:<name>")
        return
    detail = f"{_summary_line(out)} ({secs:.1f}s)"
    if code != 0:
        bad = _failed_cases(out)
        detail += "; failing cases: " + (", ".join(sorted(set(bad))[:12]) or "see log")
        print(out)
    report.add("kit", spec, code == 0, detail)


# ---- core has no per-source code -------------------------------------------------------------------------
def load_allowlist() -> dict[tuple[str, str], str]:
    """``path | literal | reason`` lines of ``.github/core-allowlist.txt``."""
    out: dict[tuple[str, str], str] = {}
    p = os.path.join(ROOT, CORE_ALLOWLIST)
    if not os.path.exists(p):
        return out
    with open(p, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = [x.strip() for x in ln.split("|", 2)]
            if len(parts) == 3 and all(parts):
                out[(parts[0], parts[1])] = parts[2]
    return out


def source_names() -> set[str]:
    """The words core must not branch on: each built-in bobbin's vendor (``google``, ``atlassian``; not the core's own
    ``hookspool.`` namespace) and full name, the code sources, and the legacy example templates (``trello``)."""
    names = set(all_code_sources())
    for name in builtin_bobbins():
        vendor = name.partition(".")[0]
        if vendor not in GENERIC_VENDORS:
            names |= {vendor, name}
    d = os.path.join(ROOT, EXAMPLES_DIR)
    if os.path.isdir(d):
        names |= {f[:-5] for f in os.listdir(d) if f.endswith(".toml")}
    return names


def core_files() -> list[str]:
    """The files that must stay source-agnostic: everything under ``hookspool/`` except the per-source plugin
    modules themselves, the bobbins (``hookspool/bobbins/<vendor>/<product>/``) and the per-provider helpers they share
    (``hookspool/providers/``). The shared machinery in ``hookspool/sources/`` (``__init__.py`` and the template
    engine ``template.py``) and ``hookspool/bobbins/__init__.py`` are core and are guarded. A vendor's
    ``hookspool/bobbins/<vendor>/__init__.py`` is empty (the kit's core-only row checks it)."""
    out = []
    for base, _dirs, files in os.walk(os.path.join(ROOT, "hookspool")):
        rel = os.path.relpath(base, ROOT).replace(os.sep, "/")
        if "__pycache__" in rel:
            continue
        if rel.startswith(BOBBINS_DIR + "/") or rel == PROVIDERS_DIR or rel.startswith(PROVIDERS_DIR + "/"):
            continue                      # a bobbin, or a provider's helpers: they name their own vendor
        for f in files:
            if not f.endswith(".py"):
                continue
            if rel == SOURCES_DIR and f[:-3] not in NOT_A_SOURCE:
                continue                  # a source plugin: it names its own vendor
            out.append(f"{rel}/{f}")
    return sorted(out)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                ids.add(id(body[0].value))
    return ids


def core_violations(names: set[str], files: list[str] | None = None,
                    allow: dict[tuple[str, str], str] | None = None) -> list[tuple[str, int, str, str]]:
    """String literals in core code (docstrings and comments excluded) that name a source: the shape of
    ``if source == "github"``, a header table keyed by a vendor, a per-vendor default. Returns
    ``(path, line, literal, source)``."""
    allow = load_allowlist() if allow is None else allow
    hits = []
    pats = {n: re.compile(rf"(?<![a-z0-9]){re.escape(n.lower())}(?![a-z0-9])") for n in names}
    for rel in files if files is not None else core_files():
        try:
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                tree = ast.parse(f.read(), rel)
        except (OSError, SyntaxError) as e:
            hits.append((rel, 0, f"unparseable: {e}", ""))
            continue
        skip = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
                low = node.value.lower()
                for n, pat in pats.items():
                    if pat.search(low) and (rel, node.value) not in allow:
                        hits.append((rel, node.lineno, node.value, n))
    return sorted(set(hits))


def run_guard(report: Report) -> None:
    names = source_names()
    hits = core_violations(names)
    for rel, line, lit, n in hits:
        report.add("core-guard", f"{rel}:{line}", False,
                   f"string {lit!r} names source '{n}'; move it into the source plugin or config")
    if not hits:
        report.add("core-guard", "hookspool/ (core)", True,
                   f"no per-source literals for {len(names)} sources ({len(load_allowlist())} allowlisted)")


# ---- secret / hostname scan ------------------------------------------------------------------------------
SECRET_PATTERNS = _scan.SECRET_PATTERNS          # one set of patterns for this scan and the bobbin kit
# Hosts that may appear: reserved names (RFC 2606/6761), single-label names, the loopback and
# documentation addresses, and the public API/docs hosts the shipped sources link to.
HOST_OK_SUFFIX = (".example", ".test", ".invalid", ".localhost", ".example.com", ".example.org", ".example.net",
                  "googleapis.com", "google.com", "github.com", "githubusercontent.com", "zoom.us", "plaid.com",
                  "atlassian.com", "trello.com", ".cloudflare.com", "kaneo.app", "standardwebhooks.com", "python.org", "pypi.org",
                  "readthedocs.io", "shields.io", "mermaid.js.org",
                  "jaredzimmerman.github.io",                       # this project's documentation site
                  "contributor-covenant.org",                       # CODE_OF_CONDUCT.md's source and FAQ
                  "w3.org", "hairline.lucasmarkes.com")      # the SVG namespace, and the style the banners follow
HOST_OK_EXACT = {"localhost", "example.com", "example.org", "example.net", "127.0.0.1", "0.0.0.0", "[::1]"}
URL_RE = re.compile(r"""\b(?:https?|wss?)://(?:[^/\s'"<>@]*@)?(\[[0-9a-fA-F:]+\]|[A-Za-z0-9.-]+)""")
# A bare host name (``server_name hooks.example.com;``, a path under /etc/nginx/sites-available/): labels, then a
# well-known public TLD. Short TLDs that are also common attribute names (``.io``, ``.app``, ``.dev``, ``.me``,
# ``.co``) are checked in non-Python files only, where a dotted name is not code.
FQDN_TLDS = "com|net|org|biz|edu|gov|us|uk|de|fr|nl|eu|ca|au|ai|cloud|xyz|online|site|tech"
FQDN_TLDS_TEXT_ONLY = "io|app|dev|me|co"
FQDN_RE = re.compile(rf"(?<![A-Za-z0-9_.-])((?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+(?:{FQDN_TLDS}))(?![A-Za-z0-9_-])",
                     )
FQDN_TEXT_RE = re.compile(
    rf"(?<![A-Za-z0-9_.-])((?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+(?:{FQDN_TLDS}|{FQDN_TLDS_TEXT_ONLY}))"
    rf"(?![A-Za-z0-9_-])")
IPV4_RE = _scan.IPV4_RE
DOC_NETS = _scan.DOC_NETS
ALLOW_MARK = "hookspool-scan: allow"
MAX_SCAN_BYTES = 8 * 1024 * 1024


def local_deny_terms(env=None, root: str = ROOT) -> list[str]:
    """Owner-specific terms, kept out of the repo: ``HOOKSPOOL_DENYLIST`` (comma separated) and the
    gitignored ``deploy/local/denylist.txt`` (one per line; ``HOOKSPOOL_DENYLIST_FILE`` overrides the path)."""
    env = os.environ if env is None else env
    terms = [t.strip() for t in env.get("HOOKSPOOL_DENYLIST", "").split(",")]
    path = env.get("HOOKSPOOL_DENYLIST_FILE") or os.path.join(root, "deploy", "local", "denylist.txt")
    try:
        with open(path, encoding="utf-8") as f:
            terms += [ln.strip() for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
    except OSError:
        pass
    return sorted({t for t in terms if t})


def host_allowed(host: str) -> bool:
    h = host.lower().rstrip(".")
    ip = IPV4_RE.fullmatch(h)
    if ip:
        return ip_allowed(*(int(x) for x in ip.groups()))
    if h in HOST_OK_EXACT or "." not in h.strip("[]"):
        return True
    return any(h == s.lstrip(".") or h.endswith(s if s.startswith(".") else "." + s) for s in HOST_OK_SUFFIX)


ip_allowed = _scan.ip_allowed


def scan_text(rel: str, text: str, deny: list[str] | None = None) -> list[tuple[int, str, str]]:
    """``(line, kind, excerpt)`` for each finding in ``text``. A line carrying ``hookspool-scan: allow``
    is skipped (use it for a documented placeholder, with the reason beside it)."""
    deny_rx = re.compile("|".join(re.escape(t) for t in deny), re.I) if deny else None
    out = []
    for i, ln in enumerate(text.splitlines(), 1):
        if ALLOW_MARK in ln:
            continue
        for kind, excerpt in _scan.secret_hits(ln, rel):
            out.append((i, kind, excerpt))
        seen_hosts = set()
        for m in URL_RE.finditer(ln):
            seen_hosts.add(m.group(1).lower())
            if not host_allowed(m.group(1)):
                out.append((i, "hostname", m.group(1)))
        for m in (FQDN_RE if rel.endswith(".py") else FQDN_TEXT_RE).finditer(ln):
            h = m.group(1)
            if h.lower() not in seen_hosts and not host_allowed(h):
                out.append((i, "hostname", h))
        for m in IPV4_RE.finditer(ln):
            nums = tuple(int(x) for x in m.groups())
            if not ip_allowed(*nums):
                out.append((i, "IP address", m.group(0)))
        if deny_rx:
            m = deny_rx.search(ln)
            if m:
                out.append((i, "denylisted term", "<local denylist>"))   # never echo the term itself
    return out


_redact = _scan.redact


def tracked_files() -> list[str]:
    """Every tracked file and every untracked one git does not ignore."""
    return sorted(n for n in _paths(_git("ls-files", "-z", "-co", "--exclude-standard")) if not n.startswith(SKIP_TREES))


def read_text(path: str) -> str | None:
    """The file as text, or None for a binary (a NUL byte), a file that is not UTF-8, or one too large.
    Whatever its extension: a key, certificate or env file is exactly what must not slip through."""
    try:
        if os.path.getsize(path) > MAX_SCAN_BYTES:
            return None
        with open(path, "rb") as f:
            raw = f.read()
        if b"\0" in raw:
            return None
        return raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def run_scan(report: Report, files: list[str]) -> None:
    deny = local_deny_terms()
    scanned = bad = 0
    for rel in files:
        path = os.path.join(ROOT, rel)
        text = read_text(path) if os.path.isfile(path) else None
        if text is None:
            continue
        scanned += 1
        for line, kind, excerpt in scan_text(rel, text, deny):
            bad += 1
            report.add("scan", f"{rel}:{line}", False, f"{kind}: {excerpt}")
    report.add("scan", f"{scanned} files", bad == 0,
               ("no secrets, hostnames or IPs" if not bad else f"{bad} finding(s)")
               + (f"; {len(deny)} local denylist term(s) applied" if deny else "; no local denylist"))


# ---- commands --------------------------------------------------------------------------------------------
def cmd_discover(args, report: Report) -> list[str]:
    files = changed_files(args.base)
    specs = discover(files)
    report.add("discover", f"{len(files)} changed files", True,
               ", ".join(specs) if specs else "no source plugin or template touched")
    return specs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="plugin_ci", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("discover", "all"):
        p = sub.add_parser(name)
        p.add_argument("--base", default=os.environ.get("BASE_SHA") or "origin/main",
                       help="merge-base ref to diff against (default: $BASE_SHA or origin/main)")
        p.add_argument("--json", action="store_true", help="discover: print the specs as JSON")
        if name == "all":
            p.add_argument("--skip-bobbins", action="store_true",
                           help="leave the bobbin scorecards to their own job (bobbin.yml)")
            p.add_argument("--all-files", action="store_true", help="scan every tracked file, not only changes")
    b = sub.add_parser("bobbins")
    b.add_argument("dirs", nargs="*", help="bobbin directories (default: the bobbins the change touches)")
    b.add_argument("--base", default=os.environ.get("BASE_SHA") or "origin/main")
    b.add_argument("--report-dir", help="write <name>.json / <name>.md scorecards and meta.json here")
    b.add_argument("--pr", default=os.environ.get("PR_NUMBER"), help="the pull request number, for meta.json")
    b.add_argument("--strict", action="store_true", help="advisory checks fail too")
    b.add_argument("--online", dest="fetch_links", action="store_true",
                   help="also fetch each bobbin's [provider] pages (the advisory provider-links row; CI passes it)")
    k = sub.add_parser("kit")
    k.add_argument("specs", nargs="+", help="template:<name> or code:<name>")
    k.add_argument("--test-file", help="code: the kit test module (default tests/test_sources_<name>.py)")
    sub.add_parser("guard")
    s = sub.add_parser("scan")
    s.add_argument("--all", action="store_true", help="every tracked file, and untracked ones git does not ignore")
    s.add_argument("--base", default=os.environ.get("BASE_SHA") or "origin/main")
    s.add_argument("paths", nargs="*", help="scan these files only")
    args = ap.parse_args(argv)

    report = Report()
    if args.cmd == "discover":
        specs = cmd_discover(args, report)
        if args.json:
            print(json.dumps(specs))
        else:
            report.emit("Plugin discovery")
        return 0
    if args.cmd == "kit":
        for spec in args.specs:
            run_kit(spec, report, args.test_file)
        report.emit("Conformance kit")
    elif args.cmd == "bobbins":
        dirs = args.dirs or discover_bobbins(changed_files(args.base))
        if not dirs:
            report.add("bobbins", "changed bobbins", True, "no bobbin touched")
        pending: list[tuple[str, dict, str]] = []
        for d in dirs:
            run_bobbin(d, report, base=args.base, strict=args.strict, report_dir=args.report_dir, pending=pending,
                       online=args.fetch_links)
        if args.report_dir:        # written after the last check: see run_bobbin on why not in between
            write_reports(args.report_dir, pending)
            write_meta(args.report_dir, args.pr, [bobbin_name(os.path.relpath(os.path.abspath(d), ROOT)) for d in dirs])
        report.emit("Bobbin scorecards")
    elif args.cmd == "guard":
        run_guard(report)
        report.emit("Core guard")
    elif args.cmd == "scan":
        files = args.paths or (tracked_files() if args.all else changed_files(args.base))
        run_scan(report, files)
        report.emit("Secret and hostname scan")
    else:   # all
        specs = cmd_discover(args, report)
        for spec in specs:
            run_kit(spec, report)
        files = changed_files(args.base)
        bobbins = [] if args.skip_bobbins else discover_bobbins(files)
        if not args.skip_bobbins:
            report.add("discover", "changed bobbins", True, ", ".join(bobbins) if bobbins else "no bobbin touched")
        for d in bobbins:
            run_bobbin(d, report, base=args.base)
        run_guard(report)
        run_scan(report, tracked_files() if args.all_files else changed_files(args.base))
        report.emit("Plugin checks")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
