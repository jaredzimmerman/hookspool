"""The bobbin conformance kit (ARCHITECTURE 14.3.5, 14.3.7, 14.3.8).

``check_bobbin(dir)`` runs every check on one bobbin directory and returns a ``Scorecard``;
``hookspool bobbin check <dir>`` prints it and CI (``.github/scripts/plugin_ci.py``) runs that same
command on every changed bobbin, so there is no check that only CI knows about.

The checks, in order (required unless marked advisory):

``manifest``    ``bobbin.toml`` is valid (14.3.2), its name is its directory, and it covers one
                service: it reads no other bobbin's signature header and calls no other bobbin's
                API host (unless both share a ``hookspool.providers`` module)
``files``       ``bobbin.toml``, ``README.md``, ``SETUP.md``, ``AGENTS.md`` and ``captures/*.http``
``todos``       no ``TODO`` placeholder is left
``core-only``   the 14.3.7 ban on every module (imports, calls, ``os.*``, ``sys.path``, another bobbin)
``docs``        SETUP.md and AGENTS.md name every secret and required setting, AGENTS.md has a
                "Needs from the human" list and numbered steps, provider-changing steps are marked
                ``outward``, and the ``[bobbins.<instance>]`` table in each expands
``captures``    one accepted and one rejected capture at least, each ``# expect:`` holds, and no
                secret-looking value or real-looking personal data
``source-kit``  the shared source kit (``hookspool.testing.sourcekit``) with each accepted capture
``processor``   (when there is one) each capture's payload through the real runtime with a fake
                context: stub HTTP, the network blocked, outputs within the declared ones, the
                journal replayed on a second run, nothing but emits on stdout, 75 and 65 exits
``tests``       the bobbin's own tests pass with the network blocked
``scope``       (advisory) whether the change touches anything outside the bobbin

Test values and stub answers for a bobbin live in ``captures/kit.toml`` (optional)::

    [secrets]                    # secret name -> TEST value (default: "test-secret-bobbin")
    secret = "test-secret-acme"
    [settings]                   # the instance settings for the kit run (default: the SETUP.md table)
    owner = "owner@example.com"
    body_signed = true           # false for a static token that does not cover the body
    timestamped = true           # default: found by replaying the accepted capture a week later
    tolerance_s = 300            # the timestamp window, when there is one
    [[http]]                     # what ctx.http answers (method + URL prefix); anything else gets {}
    method = "GET"
    url = "https://api.example.com/v1/items"
    status = 200
    json = { items = [] }

Nothing here reads a real secret: every secret is a test value written to a temporary file.
"""
from __future__ import annotations

import ast
import contextlib
import copy
import importlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from .. import bobbin_loader as BL
from .. import config as C
from ..plugin import RawRequest, Request, load_source, parse_capture
from . import scan as S
from . import sourcekit as SK
from .netblock import block_network

SCHEMA = "hookspool.bobbin-check/1"
DEFAULT_TEST_SECRET = BL.SCAFFOLD_SECRET
KIT_FILE = os.path.join("captures", "kit.toml")
KIT_KEYS = {"secrets", "settings", "body_signed", "timestamped", "tolerance_s", "http"}
HTTP_STUB_KEYS = {"method", "url", "status", "json"}
T0 = 1_800_000_000.0
EXPECTS = ("queued", "ignored", "challenge", "unverified")
REQUIRED_FILES = ("bobbin.toml", "README.md", "SETUP.md", "AGENTS.md")
TEXT_EXT = (".toml", ".md", ".py", ".http", ".txt", ".json")
PROCESSOR_TIMEOUT_S = 120.0
TESTS_TIMEOUT_S = 900.0
ROOT_PARENT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 14.3.7: the ban ---------------------------------------------------------------------------------
BANNED_MODULES = {
    "hmac": "signature checks are hookspool.verify (plugin.verify) or the template source",
    "urllib": "HTTP is ctx.http", "http": "HTTP is ctx.http", "requests": "HTTP is ctx.http",
    "httpx": "HTTP is ctx.http", "aiohttp": "HTTP is ctx.http", "socket": "HTTP is ctx.http",
    "ssl": "HTTP is ctx.http",
    "subprocess": "the dispatcher runs the work; return items", "asyncio": "the dispatcher runs the work",
    "threading": "the dispatcher runs the work", "multiprocessing": "the dispatcher runs the work",
    "signal": "the dispatcher runs the work",
    "sqlite3": "state is ctx.load_state / ctx.save_state", "fcntl": "the runtime holds the state lock",
    "tempfile": "state is ctx.load_state / ctx.save_state", "shutil": "state is ctx.load_state / ctx.save_state",
    "pickle": "state is ctx.load_state / ctx.save_state (JSON)",
    "logging": "log with ctx.log (processor) or not at all (source)",
    "importlib": "the runtime loads the bobbin's modules",
}
BANNED_CALLS = {"open": "state is ctx.load_state / ctx.save_state", "print": "return items; stdout carries only emits",
                "exec": "no dynamic code", "eval": "no dynamic code", "compile": "no dynamic code",
                "__import__": "no dynamic imports"}
BANNED_METHODS = {"write_text", "write_bytes", "open", "unlink"}


@dataclass
class Finding:
    file: str
    line: int
    message: str

    def where(self) -> str:
        return f"{self.file}:{self.line}" if self.line else self.file


@dataclass
class Check:
    name: str
    required: bool
    result: str                 # "pass" | "fail" | "skip"
    detail: str
    fix: str
    findings: list[Finding] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"name": self.name, "required": self.required, "result": self.result, "detail": self.detail,
                "fix": self.fix if self.result == "fail" else "",
                "findings": [{"file": f.file, "line": f.line, "message": f.message} for f in self.findings]}


@dataclass
class Scorecard:
    bobbin: str
    dir: str
    checks: list[Check]
    strict: bool = False

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.result == "fail" and (c.required or self.strict)]

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def scope(self) -> str | None:
        c = next((c for c in self.checks if c.name == "scope"), None)
        if c is None or c.result == "skip":
            return None
        return "bobbin-only" if c.result == "pass" else "touches-core"

    def summary(self) -> str:
        req = [c for c in self.checks if c.required]
        passed = sum(c.result == "pass" for c in req)
        skipped = sum(c.result == "skip" for c in req)
        failed = [c.name for c in self.checks if c.result == "fail" and c.required]
        adv = [c.name for c in self.checks if c.result == "fail" and not c.required]
        head = f"bobbin check {self.bobbin}: {'PASS' if self.ok else 'FAIL'}"
        if failed:
            body = f"{len(failed)} of {len(req)} required checks fail ({', '.join(failed)})"
        else:
            body = f"{passed} of {len(req)} required checks pass" + (f", {skipped} not applicable" if skipped else "")
        tail = f"; scope: {self.scope}" if self.scope else ""
        if adv and self.strict:
            tail += f"; advisory failing under --strict: {', '.join(adv)}"
        return f"{head}, {body}{tail}"

    def as_dict(self) -> dict:
        return {"schema": SCHEMA, "bobbin": self.bobbin, "dir": self.dir, "ok": self.ok, "strict": self.strict,
                "scope": self.scope, "summary": self.summary(), "checks": [c.as_dict() for c in self.checks]}


# ---- rendering ---------------------------------------------------------------------------------------
def _cell(s: str) -> str:
    return " ".join(str(s).split()).replace("|", "\\|")


def _result_label(c: dict) -> str:
    r = c["result"]
    if r == "fail":
        return "**FAIL**" if c["required"] else "advisory"
    return r


def render_md(doc: dict) -> str:
    """A scorecard (``Scorecard.as_dict()``) as GitHub Markdown: the job summary and the PR comment."""
    lines = [f"### Bobbin `{doc['bobbin']}`: {'PASS' if doc['ok'] else 'FAIL'}", "", f"`{_cell(doc['summary'])}`", "",
             "| Check | Result | Detail |", "|---|---|---|"]
    for c in doc["checks"]:
        name = c["name"] + ("" if c["required"] else " (advisory)")
        detail = c["detail"]
        if c["result"] == "fail" and c.get("fix"):
            detail += f". How to fix: {c['fix']}"
        lines.append(f"| {name} | {_result_label(c)} | {_cell(detail)} |")
    found = [(c["name"], f) for c in doc["checks"] for f in c["findings"]]
    if found:
        lines += ["", "<details><summary>Findings (file:line)</summary>", ""]
        for name, f in found[:200]:
            where = f"{f['file']}:{f['line']}" if f["line"] else f["file"]
            lines.append(f"- {name}: `{_cell(where)}` {_cell(f['message'])}")
        if len(found) > 200:
            lines.append(f"- ... and {len(found) - 200} more")
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def render_text(doc: dict) -> str:
    """A scorecard for a terminal: check | result | detail, then each finding as file:line."""
    lines = [doc["summary"], ""]
    w = max(len(c["name"]) for c in doc["checks"]) + 2
    for c in doc["checks"]:
        label = {"pass": "pass", "skip": "skip", "fail": "FAIL" if c["required"] else "warn"}[c["result"]]
        lines.append(f"{c['name']:<{w}}{label:<6}{c['detail']}")
        for f in c["findings"][:50]:
            where = f"{f['file']}:{f['line']}" if f["line"] else f["file"]
            lines.append(f"{'':<{w + 6}}{where}: {f['message']}")
        if len(c["findings"]) > 50:
            lines.append(f"{'':<{w + 6}}... and {len(c['findings']) - 50} more")
        if c["result"] == "fail" and c.get("fix"):
            lines.append(f"{'':<{w + 6}}how to fix: {c['fix']}")
    return "\n".join(lines) + "\n"


# ---- the bobbin under check --------------------------------------------------------------------------
class Bobbin:
    """One bobbin directory and what the checks share: the manifest, the kit file, a work dir."""

    def __init__(self, path: str, *, base: str | None = None, repo_root: str | None = None):
        self.dir = os.path.abspath(path)
        self.real = os.path.realpath(self.dir)
        self.dirname = os.path.basename(os.path.normpath(self.dir))
        self.builtin = os.path.dirname(self.real) == os.path.realpath(BL.BUILTIN_DIR)
        self.base = base
        self.repo_root = repo_root if repo_root is not None else find_repo_root(self.real)
        self.work = tempfile.mkdtemp(prefix="hookspool-bobbinkit-")
        self.pkg = BL.load_package(self.dir, BL.BUILTIN if self.builtin else os.path.dirname(self.dir),
                                   dict(C.DEFAULTS["spans"]))
        self.name = self.pkg.name
        self.kit, self.kit_errors = _read_kit(os.path.join(self.dir, KIT_FILE))
        self._view: str | None = None
        self._captures: list[tuple[str, RawRequest | None, str]] | None = None

    def close(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)

    def rel(self, path: str) -> str:
        """``path`` for a finding: relative to the repository when there is one, else to the bobbin."""
        root = self.repo_root
        if root and os.path.realpath(path).startswith(os.path.realpath(root) + os.sep):
            return os.path.relpath(os.path.realpath(path), os.path.realpath(root))
        return os.path.relpath(path, self.dir)

    def file(self, *parts: str) -> str:
        return os.path.join(self.dir, *parts)

    def read(self, *parts: str) -> str | None:
        try:
            with open(self.file(*parts), encoding="utf-8") as f:
                return f.read()
        except (OSError, UnicodeDecodeError):
            return None

    # files ------------------------------------------------------------------------------------------
    def py_files(self) -> list[str]:
        out = []
        for base, dirs, files in os.walk(self.dir):
            rel = os.path.relpath(base, self.dir)
            dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", "tests") and not d.startswith("."))
            if rel.split(os.sep)[0] == "tests":
                continue
            out += [os.path.join(base, f) for f in sorted(files) if f.endswith(".py")]
        return out

    def text_files(self) -> list[str]:
        out = []
        for base, dirs, files in os.walk(self.dir):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__" and not d.startswith("."))
            out += [os.path.join(base, f) for f in sorted(files) if f.endswith(TEXT_EXT)]
        return out

    def captures(self) -> list[tuple[str, RawRequest | None, str]]:
        """(path, parsed request or None, parse error) for every ``captures/*.http``."""
        if self._captures is None:
            d = self.file("captures")
            out = []
            names = sorted(f for f in os.listdir(d) if f.endswith(".http")) if os.path.isdir(d) else []
            for n in names:
                p = os.path.join(d, n)
                try:
                    with open(p, "rb") as f:
                        out.append((p, parse_capture(f.read()), ""))
                except (OSError, ValueError, UnicodeDecodeError) as e:
                    out.append((p, None, f"{e.__class__.__name__}: {str(e)[:100]}"))
            self._captures = out
        return self._captures

    # an instance of the bobbin ---------------------------------------------------------------------------
    def view(self) -> str | None:
        """A directory holding only this bobbin, for ``spool.bobbin_path`` (None for a built-in)."""
        if self.builtin:
            return None
        if self._view is None:
            self._view = os.path.join(self.work, "view")
            os.makedirs(self._view)
            os.symlink(self.real, os.path.join(self._view, self.dirname))
        return self._view

    def test_secrets(self) -> dict[str, str]:
        given = self.kit.get("secrets") if isinstance(self.kit.get("secrets"), dict) else {}
        return {n: str(given.get(n, DEFAULT_TEST_SECRET)) for n in self.pkg.secrets}

    def secret_refs(self) -> dict[str, dict]:
        d = os.path.join(self.work, "secrets")
        os.makedirs(d, exist_ok=True)
        refs = {}
        for n, v in self.test_secrets().items():
            p = os.path.join(d, n)
            with open(p, "w", encoding="utf-8") as f:
                f.write(v)
            os.chmod(p, 0o600)
            refs[n] = {"file": p}
        return refs

    def settings(self) -> dict:
        s = self.kit.get("settings")
        if isinstance(s, dict):
            return copy.deepcopy(s)
        for _path, _line, doc, _err in self.config_tables("SETUP.md"):
            inst = _instances_of(doc, self.name)
            if inst:
                st = next(iter(inst.values())).get("settings")
                return copy.deepcopy(st) if isinstance(st, dict) else {}
        return {}

    def outputs_map(self) -> dict[str, str]:
        return {k: ("hermes:kit" if k == BL.ALL_OUTPUTS else f"hermes:kit-{k}") for k in self.pkg.outputs}

    def expand(self, instance: dict, *, extra: dict | None = None) -> tuple[dict | None, list[str]]:
        """``load_config``'s expansion of ``[bobbins.<name>] = instance`` (plus ``extra`` tables)."""
        doc = copy.deepcopy(extra or {})
        doc.setdefault("bobbins", {})[self.name] = instance
        return self.expand_doc(doc)

    def expand_doc(self, doc: dict) -> tuple[dict | None, list[str]]:
        doc = copy.deepcopy(doc)
        sp = doc.setdefault("spool", {}) if isinstance(doc.get("spool", {}), dict) else {}
        doc["spool"] = sp
        sp["home"] = os.path.join(self.work, "home")
        view = self.view()
        if view:
            sp["bobbin_path"] = [view]
        else:
            sp.pop("bobbin_path", None)
        cfg = C.merge(C.DEFAULTS, doc)
        try:
            return BL.expand(cfg), []
        except BL.BobbinError as e:
            return None, list(e.errors)
        except C.ConfigError as e:
            return None, [str(e)]

    def instance(self) -> tuple[dict | None, list[str]]:
        inst = {"secrets": self.secret_refs(), "settings": self.settings(), "outputs": self.outputs_map()}
        return self.expand(inst)

    def config_tables(self, doc_name: str) -> list[tuple[str, int, dict | None, str]]:
        """Every fenced ``toml`` block of a doc that defines ``[bobbins.*]``: (path, line, parsed, error)."""
        text = self.read(doc_name)
        if text is None:
            return []
        out = []
        for m in re.finditer(r"^([ \t]*)```toml[^\n]*\n(.*?)^[ \t]*```", text, re.M | re.S):
            body = "\n".join(ln[len(m.group(1)):] if ln.startswith(m.group(1)) else ln
                             for ln in m.group(2).split("\n"))
            if not re.search(r"^\s*\[\s*bobbins\.", body, re.M):
                continue
            line = text.count("\n", 0, m.start()) + 2
            try:
                out.append((self.file(doc_name), line, tomllib.loads(body), ""))
            except tomllib.TOMLDecodeError as e:
                out.append((self.file(doc_name), line, None, str(e)))
        return out


def find_repo_root(path: str) -> str | None:
    d = path
    while True:
        if os.path.isdir(os.path.join(d, ".git")) or os.path.isfile(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def _read_kit(path: str) -> tuple[dict, list[Finding]]:
    if not os.path.exists(path):
        return {}, []
    try:
        with open(path, "rb") as f:
            kit = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        return {}, [Finding(KIT_FILE, 0, f"cannot be read: {str(e)[:120]}")]
    errs = []
    for k in kit:
        if k not in KIT_KEYS:
            errs.append(Finding(KIT_FILE, 0, f"{k}: unknown key ({', '.join(sorted(KIT_KEYS))})"))
    sec = kit.get("secrets", {})
    if not isinstance(sec, dict) or not all(isinstance(v, str) for v in sec.values()):
        errs.append(Finding(KIT_FILE, 0, "secrets: a table of secret name = test value"))
    for k in ("body_signed", "timestamped"):
        if k in kit and not isinstance(kit[k], bool):
            errs.append(Finding(KIT_FILE, 0, f"{k}: must be true or false"))
    t = kit.get("tolerance_s")
    if t is not None and (isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0):
        errs.append(Finding(KIT_FILE, 0, "tolerance_s: must be a number > 0"))
    http = kit.get("http", [])
    if not isinstance(http, list) or not all(isinstance(h, dict) and isinstance(h.get("url"), str) and
                                             not set(h) - HTTP_STUB_KEYS for h in http):
        errs.append(Finding(KIT_FILE, 0, "http: a list of { method, url, status, json } tables"))
    return kit, errs


def _instances_of(doc: dict | None, name: str) -> dict[str, dict]:
    b = (doc or {}).get("bobbins")
    if not isinstance(b, dict):
        return {}
    return {i: s for i, s in b.items() if isinstance(s, dict) and s.get("bobbin", i) == name}


def _toml_line(text: str, path: str) -> int:
    """The line of the table or key that an error's dotted path names (best effort, else 1)."""
    parts = [p.strip('"') for p in re.split(r"\.(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", path) if p]
    lines = text.splitlines()
    for n in range(len(parts), 0, -1):
        head = ".".join(parts[:n])
        for i, ln in enumerate(lines, 1):
            s = ln.strip().replace('"', "")
            if s == f"[{head}]":
                if n < len(parts):
                    for j in range(i, len(lines)):
                        t = lines[j].strip()
                        if t.startswith("["):
                            break
                        if re.match(rf"{re.escape(parts[n])}\s*=", t):
                            return j + 1
                return i
    for i, ln in enumerate(lines, 1):
        if parts and re.match(rf"\s*{re.escape(parts[-1])}\s*=", ln):
            return i
    return 1


# ---- the checks --------------------------------------------------------------------------------------
Result = tuple[str, str, list[Finding]]


def check_manifest(b: Bobbin) -> Result:
    text = b.read("bobbin.toml")
    if text is None:
        return "fail", "bobbin.toml is missing or unreadable", [Finding(b.rel(b.file("bobbin.toml")), 0, "missing")]
    rel = b.rel(b.file("bobbin.toml"))
    found = [Finding(rel, _toml_line(text, e.split(":")[0]), e) for e in b.pkg.errors]
    if not found and b.pkg.name != b.dirname:
        found.append(Finding(rel, 1, f"name: {b.pkg.name!r} must equal the directory name {b.dirname!r}"))
    if not b.builtin and os.path.isfile(os.path.join(BL.BUILTIN_DIR, b.pkg.name, BL.MANIFEST)):
        found.append(Finding(rel, 1, f"name: a built-in bobbin {b.pkg.name!r} exists; a new bobbin needs its own name"))
    found += _one_service(b)
    if found:
        return "fail", f"{len(found)} problem(s)", found
    src = "template" if "template" in b.pkg.source else "source.py"
    return "pass", (f"{b.pkg.name} {b.pkg.version}: {src}, {len(b.pkg.secrets)} secret(s), {len(b.pkg.settings)} "
                    f"setting(s), outputs {', '.join(b.pkg.outputs) or '-'}"
                    + (", processor" if b.pkg.processor is not None else "")), []


# one bobbin per service: signature headers and API hosts that belong to another bobbin
_URL_HOST = re.compile(r"""\b(?:https?|wss?)://([A-Za-z0-9.-]+)""")
_NOT_A_SERVICE_HOST = re.compile(r"(?i)(^|\.)(example\.(com|org|net)|[a-z0-9-]+\.(test|example|invalid|localhost)|"
                                 r"localhost)$|^127\.|^\[")


_PROSE_KEYS = frozenset({"description", "summary", "docs", "doc", "documentation", "homepage", "help", "title"})


@dataclass
class _Identity:
    name: str
    auth: set[str]
    reads: dict[str, tuple[str, int]]       # header (lower) -> where it is read
    hosts: dict[str, tuple[str, int]]       # host -> where it appears
    providers: set[str]


def _identity(pkg_dir: str, name: str, manifest: dict) -> _Identity:
    auth: set[str] = set()
    reads: dict[str, tuple[str, int]] = {}
    hosts: dict[str, tuple[str, int]] = {}
    providers: set[str] = set()
    src = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    tpl = src.get("template")
    if isinstance(tpl, dict) and isinstance(tpl.get("header"), str):
        auth.add(tpl["header"].lower())
    # hosts come from string constants in code and from string values in the manifest, never from comments
    # or docstrings: a documentation link in either is not a call to that host
    manifest_text = ""
    manifest_path = os.path.join(pkg_dir, BL.MANIFEST)
    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest_text = f.read()
    except (OSError, UnicodeDecodeError):
        pass
    manifest_lines = manifest_text.splitlines()

    def note_host(text: str, path: str, line: int) -> None:
        for m in _URL_HOST.finditer(text):
            h = m.group(1).lower().rstrip(".")
            if not _NOT_A_SERVICE_HOST.search(h):
                hosts.setdefault(h, (path, line))

    def manifest_strings(node: object, key: str = "") -> Iterator[str]:
        if isinstance(node, str):
            if key.lower() not in _PROSE_KEYS:
                yield node
        elif isinstance(node, dict):
            for k, v in node.items():
                yield from manifest_strings(v, str(k))
        elif isinstance(node, list):
            for v in node:
                yield from manifest_strings(v, key)

    for value in manifest_strings(manifest):
        line = next((i for i, ln in enumerate(manifest_lines, 1) if value in ln), 0)
        note_host(value, manifest_path, line)
    for base, dirs, files in os.walk(pkg_dir):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "tests")]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(base, fn)
            try:
                with open(p, encoding="utf-8") as f:
                    text = f.read()
                tree = ast.parse(text, p)
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue
            docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                          if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                          and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                    note_host(node.value, p, node.lineno)
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "auth_headers"
                                                        for t in node.targets):
                    for c in ast.walk(node.value):
                        if isinstance(c, ast.Constant) and isinstance(c.value, str):
                            auth.add(c.value.lower())
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "header" \
                        and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    reads.setdefault(node.args[0].value.lower(), (p, node.lineno))
                mod = None
                if isinstance(node, ast.ImportFrom) and node.module:
                    mod = node.module
                    if mod == "hookspool.providers":
                        providers |= {a.name for a in node.names}
                elif isinstance(node, ast.Import):
                    for a in node.names:
                        if a.name.startswith("hookspool.providers."):
                            providers.add(a.name.split(".")[2])
                if mod and mod.startswith("hookspool.providers."):
                    providers.add(mod.split(".")[2])
    return _Identity(name, auth, reads, hosts, providers)


def _other_bobbins(b: Bobbin) -> list[tuple[str, str, dict]]:
    out = []
    roots = [BL.BUILTIN_DIR] + ([os.path.dirname(b.real)] if not b.builtin else [])
    seen = {b.real}
    for root in roots:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for n in names:
            d = os.path.realpath(os.path.join(root, n))
            if d in seen or not os.path.isfile(os.path.join(d, BL.MANIFEST)):
                continue
            seen.add(d)
            try:
                with open(os.path.join(d, BL.MANIFEST), "rb") as f:
                    m = tomllib.load(f)
            except (OSError, tomllib.TOMLDecodeError):
                continue
            out.append((d, str(m.get("name") or n), m))
    return out


def _one_service(b: Bobbin) -> list[Finding]:
    me = _identity(b.dir, b.name, b.pkg.manifest)
    found = []
    for d, name, m in _other_bobbins(b):
        other = _identity(d, name, m)
        for h in sorted(other.auth - me.auth):
            if h in me.reads:
                p, line = me.reads[h]
                found.append(Finding(b.rel(p), line, f"reads {h}, the signature header of bobbin {name!r}: one bobbin"
                                                    " covers one service. If this service uses the same header name"
                                                    " (generic names are shared by unrelated providers), declare it"
                                                    " in this bobbin's auth_headers"))
        if me.providers & other.providers:
            continue
        for h in sorted(set(me.hosts) & set(other.hosts)):
            p, line = me.hosts[h]
            found.append(Finding(b.rel(p), line, f"calls {h}, the API host of bobbin {name!r}: one bobbin covers one"
                                                " service (bobbins of one provider share a hookspool.providers module)"))
    return found


def check_files(b: Bobbin) -> Result:
    missing = [f for f in REQUIRED_FILES if not os.path.isfile(b.file(f))]
    if not any(c[0].endswith(".http") for c in b.captures()):
        missing.append("captures/*.http")
    if missing:
        return "fail", "missing: " + ", ".join(missing), [Finding(b.rel(b.dir) + "/" + m, 0, "missing") for m in missing]
    return "pass", "bobbin.toml, README.md, SETUP.md, AGENTS.md, " + f"{len(b.captures())} capture(s)", []


def check_todos(b: Bobbin) -> Result:
    found = []
    for p in b.text_files():
        try:
            with open(p, encoding="utf-8") as f:
                for i, ln in enumerate(f, 1):
                    if re.search(r"\bTODO\b", ln):
                        found.append(Finding(b.rel(p), i, "TODO placeholder"))
        except (OSError, UnicodeDecodeError):
            continue
    if found:
        return "fail", f"fill in the TODOs ({len(found)} left)", found
    return "pass", "no TODO placeholders", []


class _BanWalker(ast.NodeVisitor):
    """The 14.3.7 rules on one module."""

    def __init__(self, rel: str):
        self.rel = rel
        self.found: list[Finding] = []
        self.alias: dict[str, str] = {}            # local name -> "os" | "time" | "sys" | "builtins"

    def hit(self, node: ast.AST, msg: str) -> None:
        self.found.append(Finding(self.rel, getattr(node, "lineno", 0), msg))

    def _module(self, node: ast.AST, mod: str) -> None:
        top = mod.split(".")[0]
        if top in BANNED_MODULES:
            self.hit(node, f"imports {mod}: {BANNED_MODULES[top]}")
        if mod == "hookspool.bobbins" or mod.startswith("hookspool.bobbins."):
            self.hit(node, f"imports {mod}: a bobbin never imports another bobbin, or itself by package path")

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self._module(node, a.name)
            top = a.name.split(".")[0]
            if top in ("os", "time", "sys", "builtins"):
                self.alias[a.asname or top] = top
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            self.hit(node, "relative import: a bobbin's modules do not import each other or another bobbin (the"
                           " runtime loads source.py and processor.py; shared code goes to core)")
        mod = node.module or ""
        if not node.level:
            self._module(node, mod)
            if mod == "hookspool" and any(a.name == "bobbins" for a in node.names):
                self.hit(node, "imports hookspool.bobbins: a bobbin never imports another bobbin")
            if mod == "os":
                for a in node.names:
                    if a.name != "path":
                        self.hit(node, f"imports os.{a.name}: " + _os_hint(a.name))
            if mod == "time" and any(a.name == "sleep" for a in node.names):
                self.hit(node, "imports time.sleep: never sleep; raise Retryable and the dispatcher backs off")
            if mod == "sys" and any(a.name == "path" for a in node.names):
                self.hit(node, "uses sys.path: a bobbin never changes the import path")
            if mod == "builtins":
                for a in node.names:
                    if a.name in BANNED_CALLS:
                        self.hit(node, f"imports builtins.{a.name}: {BANNED_CALLS[a.name]}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.value, ast.Name):
            mod = self.alias.get(node.value.id)
            if mod == "os" and node.attr != "path":
                self.hit(node, f"uses os.{node.attr}: " + _os_hint(node.attr))
            elif mod == "time" and node.attr == "sleep":
                self.hit(node, "calls time.sleep: never sleep; raise Retryable and the dispatcher backs off")
            elif mod == "sys" and node.attr == "path":
                self.hit(node, "uses sys.path: a bobbin never changes the import path")
            elif mod == "builtins" and node.attr in BANNED_CALLS:
                self.hit(node, f"uses builtins.{node.attr}: {BANNED_CALLS[node.attr]}")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if isinstance(f, ast.Name) and f.id in BANNED_CALLS:
            self.hit(node, f"calls {f.id}(): {BANNED_CALLS[f.id]}")
        if isinstance(f, ast.Attribute) and f.attr in BANNED_METHODS:
            self.hit(node, f"calls .{f.attr}(): a bobbin never touches files; state is ctx.load_state / ctx.save_state")
        self.generic_visit(node)


def _os_hint(attr: str) -> str:
    if attr == "environ" or attr.startswith("getenv") or attr.startswith("putenv"):
        return "settings are ctx.settings and secrets ctx.secret(); a bobbin never reads the environment"
    return "only os.path is allowed (state is ctx.load_state / ctx.save_state)"


def ban_findings(path: str, rel: str) -> list[Finding]:
    """The 14.3.7 findings for one module (a syntax error is one finding)."""
    try:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), path)
    except SyntaxError as e:
        return [Finding(rel, e.lineno or 0, f"does not parse: {e.msg}")]
    except (OSError, UnicodeDecodeError) as e:
        return [Finding(rel, 0, f"cannot be read ({e.__class__.__name__})")]
    w = _BanWalker(rel)
    w.visit(tree)
    return sorted(w.found, key=lambda f: (f.line, f.message))


def check_core_only(b: Bobbin) -> Result:
    files = b.py_files()
    found = [f for p in files for f in ban_findings(p, b.rel(p))]
    if found:
        return "fail", f"{len(found)} use(s) of something core provides", found
    return "pass", f"{len(files)} module(s) call core only" if files else "no Python modules (template only)", []


_NUMBERED = re.compile(r"^ {0,3}(\d+)[.)]\s+\S", re.M)
_OUTWARD_CMD = re.compile(r"(?i)\bcurl\b[^\n]*(?:-X\s*|--request\s+)(?:POST|PUT|PATCH|DELETE)\b|"
                          r"\bgh\s+api\b[^\n]*(?:-X\s*|--method\s+)(?:POST|PUT|PATCH|DELETE)\b|"
                          r"\bcurl\b[^\n]*\s(?:-d|--data(?:-raw|-binary)?|--json|-F|--form)\b")
_LOCAL_URL = re.compile(r"(?i)https?://(?:localhost|127\.0\.0\.1|\[::1\])")


def _outside_fences(text: str) -> str:
    return re.sub(r"```.*?```", lambda m: re.sub(r"[^\n]", " ", m.group(0)), text, flags=re.S)


def _word(name: str, text: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])", text) is not None


def check_docs(b: Bobbin) -> Result:
    docs = {n: b.read(n) for n in ("SETUP.md", "AGENTS.md")}
    if all(v is None for v in docs.values()):
        return "fail", "SETUP.md and AGENTS.md are missing", [Finding(b.rel(b.file(n)), 0, "missing") for n in docs]
    found: list[Finding] = []
    names = [("secret", n) for n in b.pkg.secrets] + [("required setting", n) for n, s in b.pkg.settings.items()
                                                      if isinstance(s, dict) and s.get("required") is True]
    for doc, text in docs.items():
        rel = b.rel(b.file(doc))
        if text is None:
            found.append(Finding(rel, 0, "missing"))
            continue
        for kind, n in names:
            if not _word(n, text):
                found.append(Finding(rel, 0, f"{kind} `{n}` is not named"))
        tables = b.config_tables(doc)
        if not tables:
            found.append(Finding(rel, 0, f"no ```toml block with the [bobbins.<instance>] table for {b.name}"))
        for path, line, parsed, err in tables:
            if parsed is None:
                found.append(Finding(rel, line, f"the config table is not valid TOML ({err[:100]}); quote placeholders"
                                                " (\"<VALUE>\")"))
                continue
            inst = _instances_of(parsed, b.name)
            others = [i for i, s in (parsed.get("bobbins") or {}).items() if i not in inst]
            if not inst:
                found.append(Finding(rel, line, f"the config table has no instance of bobbin {b.name!r}"))
                continue
            if others:
                found.append(Finding(rel, line, f"the config table also enables {', '.join(others)}; show only this"
                                                " bobbin"))
                continue
            _ex, errs = b.expand_doc(parsed)
            found += [Finding(rel, line, f"does not expand: {e}") for e in errs]
    agents = docs.get("AGENTS.md")
    if agents is not None:
        rel = b.rel(b.file("AGENTS.md"))
        plain = _outside_fences(agents)
        m = re.search(r"(?im)^[#*\s]*needs from the human\b.*$", plain)
        if not m:
            found.append(Finding(rel, 0, "no \"Needs from the human\" list"))
        else:
            after = plain[m.end():]
            nxt = re.search(r"^#{1,6} ", after, re.M)
            block = after[:nxt.start()] if nxt else after
            if not re.search(r"^\s*(?:[-*]|\d+[.)])\s+\S", block, re.M):
                found.append(Finding(rel, plain.count("\n", 0, m.start()) + 1, "the \"Needs from the human\" list is"
                                                                               " empty"))
        steps = list(_NUMBERED.finditer(plain))
        if len(steps) < 2:
            found.append(Finding(rel, 0, "no numbered steps (1. 2. ...)"))
        else:
            marked = 0
            for i, s in enumerate(steps):
                end = steps[i + 1].start() if i + 1 < len(steps) else len(agents)
                nxt = re.search(r"^#{1,6} ", plain[s.start():end], re.M)
                if nxt:
                    end = s.start() + nxt.start()
                body = agents[s.start():end]
                line = agents.count("\n", 0, s.start()) + 1
                is_outward = re.search(r"(?i)\boutward\b", body) is not None
                marked += is_outward
                for c in _OUTWARD_CMD.finditer(body):
                    if not is_outward and not _LOCAL_URL.search(body[c.start():c.start() + 300]):
                        found.append(Finding(rel, line, f"step {s.group(1)} changes something at the provider"
                                                        " but is not marked `outward`"))
                        break
            if not marked:
                found.append(Finding(rel, 0, "no step is marked `outward` (registering the webhook at the provider"
                                             " changes something there)"))
    if found:
        return "fail", f"{len(found)} problem(s) in SETUP.md / AGENTS.md", found
    return "pass", (f"{len(names)} secret(s)/required setting(s) named in both; config tables expand; "
                    "AGENTS.md has the human list, numbered steps, outward marks"), []


def check_captures(b: Bobbin) -> Result:
    caps = b.captures()
    found: list[Finding] = list(b.kit_errors)
    for f in found:
        f.file = b.rel(b.file(KIT_FILE))
    if not caps:
        return "fail", "no captures/*.http", [Finding(b.rel(b.file("captures")), 0, "no .http capture")]
    expects: dict[str, list[str]] = {}
    for p, raw, err in caps:
        rel = b.rel(p)
        if raw is None:
            found.append(Finding(rel, 1, f"does not parse ({err})"))
            continue
        exp = raw.meta.get("expect")
        if exp not in EXPECTS:
            found.append(Finding(rel, 1, f"`# expect:` must be one of {', '.join(EXPECTS)}"))
        else:
            expects.setdefault(exp, []).append(p)
        try:
            with open(p, encoding="utf-8") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError):
            text = ""
        scan_rel = f"bobbins/{b.name}/captures/{os.path.basename(p)}"
        for line, kind, ex in S.secret_findings(text, scan_rel):
            found.append(Finding(rel, line, f"{kind}: {ex} (captures hold test secrets only)"))
        for line, kind, ex in S.personal_findings(text):
            found.append(Finding(rel, line, f"{kind}: {ex} (redact real people: example.com addresses, 555-01xx)"))
    for n, v in b.test_secrets().items():
        if "test" not in v.lower():
            found.append(Finding(b.rel(b.file(KIT_FILE)), 0, f"secrets.{n}: a kit secret must say it is a test value"
                                                             " (contain \"test\")"))
    if "queued" not in expects:
        found.append(Finding(b.rel(b.file("captures")), 0, "no accepted capture (`# expect: queued`)"))
    if "unverified" not in expects:
        found.append(Finding(b.rel(b.file("captures")), 0, "no rejected capture (`# expect: unverified`, a bad"
                                                           " signature)"))
    # each capture's expect
    ran = 0
    if not b.pkg.errors and any(raw is not None for _p, raw, _e in caps):
        kitsrc, err = _source_kit_base(b)
        if kitsrc is None:
            found.append(Finding(b.rel(b.file("bobbin.toml")), 0, f"the captures cannot run: {err}"))
        else:
            for p, raw, _e in caps:
                exp = raw.meta.get("expect") if raw is not None else None
                if exp not in EXPECTS:
                    continue
                got = _source_test(b, kitsrc, raw)
                ran += 1
                if got != exp:
                    found.append(Finding(b.rel(p), 1, f"expected {exp}, got {got}"))
    if found:
        return "fail", f"{len(found)} problem(s)", found
    counts = ", ".join(f"{len(v)} {k}" for k, v in sorted(expects.items()))
    return "pass", f"{len(caps)} capture(s) ({counts}); every `# expect:` holds; no secrets or personal data", []


# ---- running the source -----------------------------------------------------------------------------------
@dataclass
class _KitSource:
    module: Any
    source_cfg: dict
    auth_headers: tuple[str, ...]
    body_signed: bool
    unauth: bool


def _private(b: Bobbin, module: str) -> str:
    return f"_hookspool_bobbinkit.{b.name.replace('-', '_')}.{module}"


def _load_module(b: Bobbin, module: str):
    """A module of the bobbin: by name for a built-in, by file path (private name) otherwise."""
    if b.builtin:
        return importlib.import_module(f"{BL.BUILTIN_MODULE}.{b.dirname}.{module}")
    path = os.path.join(b.real, module + ".py")
    spec = importlib.util.spec_from_file_location(_private(b, module), path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {module}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    prev, sys.dont_write_bytecode = sys.dont_write_bytecode, True     # never write into the bobbin's directory
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    finally:
        sys.dont_write_bytecode = prev
    return mod


def _source_kit_base(b: Bobbin) -> tuple[_KitSource | None, str]:
    cfg, errs = b.instance()
    if cfg is None:
        return None, "the kit instance does not expand: " + "; ".join(errs)[:300] + \
            " (set required settings in captures/kit.toml [settings])"
    src = copy.deepcopy(cfg["sources"][b.name])
    src.pop("secrets", None)
    src["action"] = f"hermes:{b.name}"
    tpl = b.pkg.source.get("template")
    try:
        if isinstance(tpl, dict):
            from ..sources import template as T
            module = T.bind(b.name, src)
            body_signed = tpl.get("scheme") not in ("shared-secret-header", "url-token")
        else:
            mod = b.pkg.source["module"]
            module = _load_module(b, mod) if BL._is_local_module(mod) else importlib.import_module(mod)
            body_signed = bool(b.kit.get("body_signed", True))
    except Exception as e:  # noqa: BLE001 - reported as a finding
        return None, f"the source does not load ({e.__class__.__name__}: {str(e)[:200]})"
    auth = tuple(getattr(module, "auth_headers", ()) or ())
    missing = [s for s in getattr(module, "secrets", ()) or () if s not in b.pkg.secrets]
    if missing:
        return None, f"the source needs secret(s) the manifest does not declare: {', '.join(missing)}"
    return _KitSource(module, src, auth, body_signed, bool(getattr(module, "challenge_unauthenticated", False))), ""


def _at_path(b: Bobbin, raw: RawRequest) -> RawRequest:
    """The capture sent to ``/hooks/<name>`` (the kit's instance), keeping its query string."""
    path, q, query = raw.target.partition("?")
    want = f"/hooks/{b.name}"
    if path == want:
        return raw
    return RawRequest(raw.method, want + (q + query if q else ""), list(raw.headers), raw.body, dict(raw.meta))


def _unsigned(raw: RawRequest, auth: tuple[str, ...]) -> RawRequest:
    """The capture without its credential: the auth headers, Authorization, and the query string."""
    drop = {h.lower() for h in auth} | {"authorization", "proxy-authorization", "cookie"}
    return RawRequest(raw.method, raw.target.split("?", 1)[0], [(k, v) for k, v in raw.headers if k.lower() not in drop],
                      raw.body, {})


def _env(b: Bobbin, ks: _KitSource, sample: RawRequest, signer, work: str, clock: SK.FakeClock,
         handshake=None, unauth=None, timestamped=False) -> tuple[SK.Kit, Callable[[], SK.Env]]:
    kit = SK.Kit(name=b.name, module=ks.module, secrets=b.test_secrets(), sample=sample, sign=signer,
                 source_cfg=copy.deepcopy(ks.source_cfg), handshake=handshake, unauth_handshake=unauth,
                 timestamped=timestamped, tolerance_s=float(b.kit.get("tolerance_s", 300.0)),
                 body_signed=ks.body_signed)
    return kit, lambda: SK.Env(kit, work, clock)


def _capture_now(raw: RawRequest) -> float:
    try:
        return float(raw.meta.get("now") or T0)
    except ValueError:
        return T0


def _source_test(b: Bobbin, ks: _KitSource, raw: RawRequest, now: float | None = None) -> str:
    raw = _at_path(b, raw)
    t = _capture_now(raw) if now is None else now
    clock = SK.FakeClock(t)
    work = tempfile.mkdtemp(dir=b.work)
    _kit, make = _env(b, ks, _unsigned(raw, ks.auth_headers), lambda r, s, a: r, work, clock)
    env = make()
    try:
        with block_network():
            from .. import intake as I
            out = I.source_test(env.cfg, b.name, raw, now=t, loader=env._load)
        return str(out.get("result"))
    except Exception as e:  # noqa: BLE001 - a crash is a result too
        return f"an error ({e.__class__.__name__})"
    finally:
        env.close()


class _ReplaySigner:
    """Signs by replaying a capture: the capture cannot be re-signed, so "signed at time ``at``"
    moves the receiving clock to the capture's own time plus the same offset."""

    def __init__(self, clock: SK.FakeClock, signed: list[tuple[RawRequest, RawRequest, float]]):
        self.clock = clock
        self.table = {(u.method, u.target, u.body): (s, t) for u, s, t in signed}

    def __call__(self, raw: RawRequest, secrets, at: float) -> RawRequest:
        hit = self.table.get((raw.method, raw.target.split("?", 1)[0], raw.body))
        if hit is None:
            return raw
        signed, t = hit
        self.clock.t = t + (self.clock() - at)
        return RawRequest(signed.method, signed.target, list(signed.headers), signed.body, {})


def check_source_kit(b: Bobbin) -> Result:
    if b.pkg.errors:
        return "skip", "the manifest is invalid (see manifest)", []
    caps = [(p, _at_path(b, raw)) for p, raw, _e in b.captures() if raw is not None]
    accepted = [(p, r) for p, r in caps if r.meta.get("expect") == "queued"]
    if not accepted:
        return "fail", "no accepted capture to run the kit with", [Finding(b.rel(b.file("captures")), 0,
                                                                           "no `# expect: queued` capture")]
    ks, err = _source_kit_base(b)
    if ks is None:
        return "fail", err, [Finding(b.rel(b.file("bobbin.toml")), 0, err)]
    hs = [(p, r) for p, r in caps if r.meta.get("expect") == "challenge"]
    signed_hs = [(p, r) for p, r in hs if not ks.unauth]
    unauth_hs = next((r for p, r in hs if ks.unauth), None)
    found: list[Finding] = []
    tally = {"pass": 0, "skip": 0, "fail": 0}
    skips: set[str] = set()
    network: list[str] = []
    for p, raw in accepted:
        t = _capture_now(raw)
        if "timestamped" in b.kit:
            stamped = bool(b.kit["timestamped"])
        else:
            stamped = _source_test(b, ks, raw, now=t + 7 * 86400) != "queued"
        sample = _unsigned(raw, ks.auth_headers)
        handshake = _unsigned(signed_hs[0][1], ks.auth_headers) if signed_hs else None
        table = [(sample, raw, t)] + ([(handshake, signed_hs[0][1], _capture_now(signed_hs[0][1]))] if handshake else [])
        unauth = None
        if unauth_hs is not None:
            unauth = RawRequest(unauth_hs.method, unauth_hs.target, list(unauth_hs.headers), unauth_hs.body, {})
        for case in SK.CASES:
            cname = case.__name__[5:]
            clock = SK.FakeClock(t)
            work = tempfile.mkdtemp(dir=b.work)
            kit, _make = _env(b, ks, sample, _ReplaySigner(clock, table), work, clock, handshake=handshake,
                              unauth=unauth, timestamped=stamped)
            try:
                with block_network() as attempts:
                    SK.run(case, kit, work, clock)
                network += attempts
                tally["pass"] += 1
            except SK.Skip as e:
                tally["skip"] += 1
                skips.add(str(e))
            except AssertionError as e:
                tally["fail"] += 1
                msg = " ".join(str(e).split())[:200] or "assertion failed"
                hint = ""
                if cname == "tampered" and b.kit.get("body_signed", True) and "template" not in b.pkg.source:
                    hint = " (a static token that does not cover the body: set body_signed = false in captures/kit.toml)"
                if cname in ("stale", "future"):
                    hint = " (the window is tolerance_s in captures/kit.toml, default 300)"
                found.append(Finding(b.rel(p), 1, f"{cname}: {msg}{hint}"))
            except Exception as e:  # noqa: BLE001 - a crash in the kit run is a failure of this case
                tally["fail"] += 1
                found.append(Finding(b.rel(p), 1, f"{cname}: {e.__class__.__name__}: {' '.join(str(e).split())[:160]}"))
    if network:
        found.append(Finding(b.rel(b.dir), 0, f"the source opened network connection(s) to {', '.join(sorted(set(network))[:3])}"))
    detail = (f"{len(SK.CASES)} cases x {len(accepted)} accepted capture(s): {tally['pass']} passed, {tally['skip']} not"
              f" applicable, {tally['fail']} failed")
    if skips:
        detail += " (" + "; ".join(sorted(skips))[:200] + ")"
    if found:
        return "fail", detail, found
    return "pass", detail, []


@contextlib.contextmanager
def _quiet() -> Iterator[None]:
    """The intake logs to the ``hookspool`` logger; the kit's own handler records them, nothing prints."""
    import logging
    lg = logging.getLogger("hookspool")
    prev = lg.propagate
    lg.propagate = False
    try:
        yield
    finally:
        lg.propagate = prev


# ---- the processor ----------------------------------------------------------------------------------------
def _stage(b: Bobbin, ks: _KitSource, raw: RawRequest) -> tuple[dict, str, str] | None:
    """(payload, key, event_type) of an accepted capture, as the intake stores it."""
    from .. import intake as I
    t = _capture_now(raw)
    clock = SK.FakeClock(t)
    work = tempfile.mkdtemp(dir=b.work)
    _kit, make = _env(b, ks, _unsigned(raw, ks.auth_headers), lambda r, s, a: r, work, clock)
    env = make()
    try:
        src = load_source(b.name, C.source_cfg(env.cfg, b.name), env._load)
        req = Request(raw.method, raw.target, raw.headers, raw.body, now=t, config=src.public_config(),
                      remote_addr="capture")
        st = I.run_stages(src, req, env.cfg)
        if st.kind != "ready":
            return None
        f = st.fields
        return f["payload"], str(f["key"]), str(f.get("event_type") or "")
    except Exception:  # noqa: BLE001 - the captures check reports a capture that does not stage
        return None
    finally:
        env.close()


def _proc_env(b: Bobbin, cfg: dict, i: int, state_dir: str | None) -> dict:
    dest = cfg["destinations"][f"script:{b.name}"]
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": b.work, "LANG": "C.UTF-8"}
    env.update({k: v for k, v in (dest.get("env") or {}).items() if isinstance(v, str)})
    env["HOOKSPOOL_BOBBIN_DIR"] = b.real
    env["HOOKSPOOL_BATCH_ID"] = f"kit-{i}"
    env["HOOKSPOOL_DELIVERY_ID"] = f"hs_kit_{i}"
    if state_dir:
        env["HOOKSPOOL_STATE_DIR"] = state_dir
    return env


def _run_processor(b: Bobbin, env: dict, doc: dict, mode: str, via_http: bool, now: float) -> dict:
    work = tempfile.mkdtemp(dir=b.work)
    spec_path, report_path = os.path.join(work, "spec.json"), os.path.join(work, "report.json")
    with open(spec_path, "w", encoding="utf-8") as f:
        json.dump({"instance": b.name, "mode": mode, "fault_via_http": via_http, "now": now,
                   "http": b.kit.get("http") or [], "report": report_path}, f)
    code = ("import sys; sys.path.insert(0, " + repr(ROOT_PARENT) + "); "
            "from hookspool.testing import _procrun; sys.exit(_procrun.main(sys.argv[1:]))")
    timeout = min(float((b.pkg.processor or {}).get("timeout_s") or PROCESSOR_TIMEOUT_S), PROCESSOR_TIMEOUT_S)
    try:
        p = subprocess.run([sys.executable, "-I", "-B", "-c", code, spec_path], input=json.dumps(doc), env=env,
                           capture_output=True, text=True, timeout=timeout, cwd=work)
        rc, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        rc, out, err = -1, "", f"timed out after {timeout:.0f}s"
    try:
        with open(report_path, encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, ValueError):
        report = {}
    return {"rc": rc, "stdout": out, "stderr": err, "report": report}


def _tail(err: str) -> str:
    lines = [ln for ln in err.strip().splitlines() if ln.strip()]
    return " | ".join(" ".join(ln.split())[:160] for ln in lines[-2:])


def _non_emit_lines(out: str) -> list[int]:
    bad = []
    for i, ln in enumerate(out.splitlines(), 1):
        if not ln.strip():
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            bad.append(i)
            continue
        if not (isinstance(obj, dict) and list(obj) == ["enqueue"] and isinstance(obj["enqueue"], dict)):
            bad.append(i)
    return bad


def check_processor(b: Bobbin) -> Result:
    if b.pkg.processor is None:
        return "skip", "no processor", []
    if b.pkg.errors:
        return "skip", "the manifest is invalid (see manifest)", []
    ks, err = _source_kit_base(b)
    if ks is None:
        return "fail", err, [Finding(b.rel(b.file("bobbin.toml")), 0, err)]
    cfg, errs = b.instance()
    if cfg is None:
        return "fail", "the kit instance does not expand", [Finding(b.rel(b.file("bobbin.toml")), 0, e) for e in errs]
    accepted = [(p, _at_path(b, r)) for p, r, _e in b.captures() if r is not None and r.meta.get("expect") == "queued"]
    if not accepted:
        return "fail", "no accepted capture to run through process", []
    state = (b.pkg.processor or {}).get("state") is True
    declared = set(b.pkg.outputs)
    found: list[Finding] = []
    runs = 0
    for i, (p, raw) in enumerate(accepted, 1):
        rel = b.rel(p)
        staged = _stage(b, ks, raw)
        if staged is None:
            found.append(Finding(rel, 1, "the capture does not reach the processor (see captures)"))
            continue
        payload, key, et = staged
        now = _capture_now(raw)
        doc = {"source": b.name, "route": b.name, "key": key, "batch_id": i, "delivery_id": f"hs_kit_{i}",
               "attempt": 1, "count": 1, "first_at": now, "last_at": now, "event_type": et,
               "event_types": {et: 1} if et else {}, "summary": f"1 {b.name} event(s) for {key}",
               "content_hash": "", "events": [payload], "event_meta": [{"event_type": et}]}
        sdir = os.path.join(b.work, f"state-{i}") if state else None
        env = _proc_env(b, cfg, i, sdir)
        first = _run_processor(b, env, doc, "normal", False, now)
        runs += 1
        rep = first["report"]
        if first["rc"] != 0:
            found.append(Finding(rel, 1, f"process exited {first['rc']} on this capture: {_tail(first['stderr'])}"))
            continue
        if rep.get("network"):
            found.append(Finding(rel, 1, "the processor opened a network connection (to "
                                 + ", ".join(sorted(set(rep["network"]))[:3]) + "); use ctx.http"))
        bad = _non_emit_lines(first["stdout"])
        if bad:
            found.append(Finding(rel, 1, f"stdout line(s) {', '.join(map(str, bad[:5]))} are not emits: only the"
                                         " runtime writes to stdout"))
        extra = sorted(set(rep.get("event_types") or []) - declared) if BL.ALL_OUTPUTS not in declared else []
        if extra:
            found.append(Finding(b.rel(b.file("bobbin.toml")), 1, f"process returned event type(s) {', '.join(extra)}"
                                 " that are not declared outputs"))
        second = _run_processor(b, env, doc, "normal", False, now)
        runs += 1
        if second["rc"] != 0:
            found.append(Finding(rel, 1, f"the second run of the same batch exited {second['rc']}: "
                                         f"{_tail(second['stderr'])}"))
        elif state and (second["report"].get("process_calls") or "replaying the journal" not in second["stderr"]):
            found.append(Finding(rel, 1, "the second run of the same batch did not replay the journal"))
        elif second["stdout"] != first["stdout"]:
            found.append(Finding(rel, 1, "the second run of the same batch printed different emits"))
        if i == 1:
            via_http = bool(rep.get("http_calls"))
            for mode, want in (("raise", 75), ("permanent", 65)):
                fenv = _proc_env(b, cfg, 1000 + i, os.path.join(b.work, f"state-{mode}") if state else None)
                r = _run_processor(b, fenv, doc, mode, via_http, now)
                runs += 1
                if r["rc"] != want:
                    how = "ctx.http raised " + ("Retryable" if mode == "raise" else "Permanent") if via_http else \
                        ("process raised" if mode == "raise" else "ctx.fail_permanent was called")
                    found.append(Finding(rel, 1, f"when {how} the batch exited {r['rc']}, not {want}: let failures"
                                                 " raise (Retryable / Permanent) to the runtime"))
    detail = (f"{runs} run(s) over {len(accepted)} capture(s): stub HTTP, network blocked, outputs declared, "
              + ("journal replayed" if state else "no state (a retry calls process again)") + ", exits 75/65")
    if found:
        return "fail", f"{len(found)} problem(s)", found
    return "pass", detail, []


# ---- the bobbin's own tests -------------------------------------------------------------------------------
def own_tests(b: Bobbin) -> list[str]:
    out = []
    d = b.file("tests")
    if os.path.isdir(d) and any(f.startswith("test_") and f.endswith(".py") for f in os.listdir(d)):
        out.append(d)
    if b.repo_root:
        f = os.path.join(b.repo_root, "tests", "bobbins", f"test_{b.name.replace('-', '_')}.py")
        if os.path.isfile(f) and b.builtin:
            out.append(f)
    return out


def check_tests(b: Bobbin) -> Result:
    paths = own_tests(b)
    if not paths:
        where = f"{b.rel(b.file('tests'))}/" + (f" or tests/bobbins/test_{b.name.replace('-', '_')}.py" if b.builtin else "")
        return "skip", f"no tests of its own ({where})", []
    cwd = b.repo_root or b.dir
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT_PARENT + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    t0 = time.monotonic()
    try:
        p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header",
                            "-p", "hookspool.testing.netblock", *paths], cwd=cwd, env=env, capture_output=True,
                           text=True, timeout=TESTS_TIMEOUT_S)
        rc, out = p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        rc, out = -1, f"timed out after {TESTS_TIMEOUT_S:.0f}s"
    lines = [ln for ln in out.strip().splitlines() if re.search(r"\b(passed|failed|error|no tests ran)\b", ln)]
    summary = (lines[-1].strip(" =") if lines else "no pytest summary") + f" ({time.monotonic() - t0:.1f}s)"
    rels = ", ".join(b.rel(x) for x in paths)
    if rc != 0:
        failed = re.findall(r"^(?:FAILED|ERROR) (\S+)", out, re.M)[:10]
        found = [Finding(f.split("::")[0], 0, "failed: " + f.split("::", 1)[-1]) for f in failed]
        if "network attempt(s) blocked" in out:
            found.append(Finding(rels, 0, "a test tried to reach the network"))
        return "fail", f"{rels}: {summary}", found or [Finding(rels, 0, _tail(out))]
    return "pass", f"{rels}: {summary}", []


# ---- scope (advisory) -------------------------------------------------------------------------------------
def changed_files(root: str, base: str) -> list[str]:
    def git(*args: str) -> list[str]:
        p = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.strip()[:200])
        return [x for x in p.stdout.split("\n") if x]
    names = set(git("diff", "--name-only", "--diff-filter=ACMRD", f"{base}...HEAD"))
    names |= set(git("diff", "--name-only", "--diff-filter=ACMRD", "HEAD"))
    names |= set(git("ls-files", "--others", "--exclude-standard"))
    return sorted(n for n in names if not n.startswith((".ladder/", "deploy/local/")))


def check_scope(b: Bobbin) -> Result:
    if not b.base:
        return "skip", "no --base given (the files a change touches are found against it)", []
    if not b.repo_root:
        return "skip", "not in a git checkout", []
    try:
        files = changed_files(b.repo_root, b.base)
    except RuntimeError as e:
        return "skip", f"git: {e}", []
    mine = os.path.relpath(b.real, os.path.realpath(b.repo_root)).replace(os.sep, "/") + "/"
    own_test = f"tests/bobbins/test_{b.name.replace('-', '_')}.py"
    outside = [f for f in files if not f.startswith(mine) and f != own_test]
    if outside:
        return "fail", f"touches core: {len(outside)} file(s) outside {mine}", [Finding(f, 0, "changed outside the"
                                                                                       " bobbin") for f in outside[:50]]
    return "pass", f"bobbin-only: every changed file is under {mine}" + (f" or {own_test}" if own_test in files else ""), []


CHECKS: list[tuple[str, bool, str, Callable[[Bobbin], Result]]] = [
    ("manifest", True, "fix bobbin.toml as listed (14.3.2); one bobbin covers one service, and code two bobbins share"
                       " moves to core", check_manifest),
    ("files", True, "add bobbin.toml, README.md, SETUP.md, AGENTS.md and captures/*.http (14.3.1)", check_files),
    ("todos", True, "fill in the TODOs", check_todos),
    ("core-only", True, "call core instead (14.3.7): hookspool.verify, ctx.http, ctx.load_state / ctx.save_state,"
                        " ctx.log, ctx.settings; if core lacks something, add it to core in its own PR",
     check_core_only),
    ("docs", True, "name every secret and required setting in SETUP.md and AGENTS.md; give AGENTS.md a \"Needs from"
                   " the human\" list and numbered steps, provider-changing steps marked `outward`; keep the"
                   " [bobbins.<instance>] table valid (14.3.8)", check_docs),
    ("captures", True, "add an accepted and a bad-signature capture whose `# expect:` holds; test secrets and"
                       " example.com addresses only", check_captures),
    ("source-kit", True, "make the source pass the shared source kit with each accepted capture"
                         " (docs/ADDING_A_SOURCE.md, The conformance kit); test values go in captures/kit.toml",
     check_source_kit),
    ("processor", True, "return items of declared outputs only, go through ctx (no network, no stdout) and let"
                        " failures raise; stub API answers go in captures/kit.toml [[http]]", check_processor),
    ("tests", True, "make the bobbin's own tests pass without the network (a local stub server is fine)", check_tests),
    ("scope", False, "a change outside the bobbin directory is reviewed as a core change; split it into its own PR"
                     " when it can stand alone", check_scope),
]
CHECK_NAMES = [c[0] for c in CHECKS]


def check_bobbin(path: str, *, base: str | None = None, strict: bool = False, repo_root: str | None = None,
                 only: list[str] | None = None) -> Scorecard:
    """Every check on the bobbin at ``path`` (``only`` limits it to the named checks)."""
    if not __debug__:
        raise RuntimeError("the bobbin kit needs assertions: run it without -O")
    b = Bobbin(path, base=base, repo_root=repo_root)
    try:
        with _quiet():
            return _run_checks(b, only, strict)
    finally:
        b.close()


def _run_checks(b: Bobbin, only: list[str] | None, strict: bool) -> Scorecard:
    checks = []
    for name, required, fix, fn in CHECKS:
        if only is not None and name not in only:
            continue
        try:
            result, detail, found = fn(b)
        except Exception as e:  # noqa: BLE001 - a check that crashes fails, with its class only
            result, detail, found = "fail", f"the check crashed ({e.__class__.__name__}: {str(e)[:160]})", []
        checks.append(Check(name, required, result, detail, fix, found))
    return Scorecard(b.name, b.rel(b.dir) if b.repo_root else b.dir, checks, strict)


def main(path: str, *, report: str = "text", strict: bool = False, base: str | None = None, out=None) -> int:
    """``hookspool bobbin check <dir>``: print the scorecard; 0 when every required check passes."""
    out = out or sys.stdout
    if not os.path.isdir(path) or not os.path.isfile(os.path.join(path, BL.MANIFEST)):
        sys.stderr.write(f"hookspool: {path} is not a bobbin directory (no {BL.MANIFEST})\n")
        return 2
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):            # nothing a bobbin prints may corrupt the report
        card = check_bobbin(path, base=base, strict=strict)
    doc = card.as_dict()
    if report == "json":
        out.write(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    elif report == "md":
        out.write(render_md(doc))
    else:
        out.write(render_text(doc))
    return 0 if card.ok else 1
