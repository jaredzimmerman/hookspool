"""The bobbin conformance kit (``hookspool.testing.bobbinkit``, ARCHITECTURE 14.3.5, 14.3.7, 14.3.8).

Every check red and green: the fixture bobbins in ``tests/fixtures/bobbinkit`` pass everything, and a
copy with one thing broken fails exactly that check, with a file:line finding. Also the scorecard (text,
Markdown, the JSON schema), ``hookspool bobbin check`` and the ``bobbin new`` scaffold.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import kits  # noqa: E402

from hookspool import bobbin_loader as BL  # noqa: E402
from hookspool.__main__ import main as cli  # noqa: E402
from hookspool.testing import bobbinkit as K  # noqa: E402
from hookspool.testing import scan as S  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = "acme"                                    # the fixtures are acme.kitcode, acme.kitproc and acme.kittpl
FIX = os.path.join(ROOT, "tests", "fixtures", "bobbinkit", VENDOR)
FIXTURES = sorted(d for d in os.listdir(FIX) if os.path.isfile(os.path.join(FIX, d, "bobbin.toml")))


# ---- helpers -------------------------------------------------------------------------------------------
def copy(tmp_path, name: str, as_name: str | None = None) -> str:
    """A fixture copied to ``<tmp>/bobbins/acme/<as_name or name>`` (a bobbin_path root, then <vendor>/<product>)."""
    d = os.path.join(str(tmp_path), "bobbins", VENDOR, as_name or name)
    shutil.copytree(os.path.join(FIX, name), d)
    if as_name:
        for rel in ("bobbin.toml", "source.py", "README.md", "SETUP.md", "AGENTS.md"):
            p = os.path.join(d, rel)
            if os.path.exists(p):
                text = (open(p).read().replace(f"{VENDOR}.{name}", f"{VENDOR}.{as_name}").replace(f'"{name}"', f'"{as_name}"')
                        .replace(f"bobbins.{name}]", f"bobbins.{as_name}]"))
                open(p, "w").write(text)
        cdir = os.path.join(d, "captures")
        for f in os.listdir(cdir):
            p = os.path.join(cdir, f)
            open(p, "w").write(open(p).read().replace(f"/hooks/{name} ", f"/hooks/{as_name} "))
    return d


def edit(d: str, rel: str, old: str, new: str, count: int = 1) -> None:
    p = os.path.join(d, rel)
    text = open(p).read()
    assert old in text, (rel, old)
    open(p, "w").write(text.replace(old, new, count) if count else text.replace(old, new))


def write(d: str, rel: str, text: str) -> None:
    p = os.path.join(d, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write(text)


def run(d: str, *names: str, **kw) -> dict[str, K.Check]:
    card = K.check_bobbin(d, only=list(names), **kw)
    return {c.name: c for c in card.checks}


def messages(c: K.Check) -> list[str]:
    return [f.message for f in c.findings]


def capture(name: str, body: str, header: str, value: str, expect: str, now: int = 1800000000) -> str:
    return (f"# capture: test\n# now: {now}\n# expect: {expect}\nPOST /hooks/{name} HTTP/1.1\n"
            f"Content-Type: application/json\n{header}: {value}\nContent-Length: {len(body)}\n\n{body}\n")


# ---- the fixtures pass ------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", FIXTURES)
def test_the_fixture_bobbins_pass_every_check(name):
    card = K.check_bobbin(os.path.join(FIX, name))
    assert [c.name for c in card.checks] == K.CHECK_NAMES
    assert card.ok, K.render_text(card.as_dict())
    by = {c.name: c for c in card.checks}
    for n in ("name", "manifest", "provider", "files", "todos", "core-only", "docs", "captures", "source-kit"):
        assert by[n].result == "pass", (n, by[n].detail)
    assert by["processor"].result == ("pass" if name == "kitproc" else "skip")
    assert by["scope"].result == "skip" and not by["scope"].required
    assert by["provider-links"].result == "skip" and not by["provider-links"].required     # offline by default
    assert card.summary().startswith(f"bobbin check {VENDOR}.{name}: PASS")


def test_there_are_fixtures_of_each_kind():
    assert FIXTURES == ["kitcode", "kitproc", "kittpl"]


# ---- manifest -----------------------------------------------------------------------------------------------
def test_manifest_red_unknown_key_with_its_line(tmp_path):
    d = copy(tmp_path, "kitcode")
    edit(d, "bobbin.toml", '[secrets.secret]\ndescription = "the signing secret"',
         '[secrets.secret]\ndescription = "the signing secret"\nbogus = 1')
    c = run(d, "manifest")["manifest"]
    assert c.result == "fail" and c.required
    f, = c.findings
    assert f.file == "bobbin.toml" and f.line == 19 and "secrets.secret.bogus: unknown key" in f.message


def test_name_red_name_is_not_the_directory(tmp_path):
    d = copy(tmp_path, "kitcode")
    edit(d, "bobbin.toml", 'name = "acme.kitcode"', 'name = "acme.othername"')
    cs = run(d, "name", "manifest")
    c = cs["name"]
    assert c.result == "fail" and c.required
    f, = c.findings
    assert f.file == "bobbin.toml" and f.line == 1 and "must live in acme/othername/" in f.message
    assert cs["manifest"].result == "pass", "a name problem is the name row's, reported once"


def test_manifest_red_reading_another_bobbins_signature_header(tmp_path):
    copy(tmp_path, "kitcode")
    d = copy(tmp_path, "kitcode", "kitother")
    edit(d, "source.py", 'auth_headers = ("X-Kitcode-Signature",)', 'auth_headers = ("X-Other-Signature",)')
    edit(d, "source.py", 'return v.hmac_prefixed(req.header("X-Kitcode-Signature")',
         'return v.hmac_prefixed(req.header("X-Other-Signature") or req.header("X-Kitcode-Signature")')
    c = run(d, "manifest")["manifest"]
    assert c.result == "fail"
    f, = c.findings
    assert f.file == "source.py" and f.line > 0
    assert "x-kitcode-signature, the signature header of bobbin 'acme.kitcode'" in f.message
    assert "declare it in this bobbin's auth_headers" in f.message, "the fix for a shared generic header name"


@pytest.mark.parametrize("module", ["hookspool.bobbins.other_bobbin.source", "hookspool.bobbins",
                                    "_hookspool_bobbins.other_bobbin.source", "_hookspool_bobbinkit.other_bobbin.source"])
def test_manifest_red_source_module_that_is_another_bobbins_code(tmp_path, module):
    d = copy(tmp_path, "kitcode")
    edit(d, "bobbin.toml", 'module = "source"', f'module = "{module}"')
    c = run(d, "manifest")["manifest"]
    assert c.result == "fail"
    f, = c.findings
    assert f.file == "bobbin.toml" and f.line == 13, f.where()         # the ``module =`` line
    assert f"source.module: {module!r} is another bobbin's code" in f.message
    assert "never imports or runs another bobbin" in f.message
    assert any("another bobbin" in e for e in BL.load_package(d, "test", {}).errors), "the loader refuses it too"


def test_manifest_red_another_bobbins_api_host_unless_they_share_a_provider_module(tmp_path):
    a = copy(tmp_path, "kitcode")
    b = copy(tmp_path, "kitcode", "kitother")
    edit(b, "source.py", 'auth_headers = ("X-Kitcode-Signature",)', 'auth_headers = ("X-Other-Signature",)')
    edit(b, "source.py", 'req.header("X-Kitcode-Signature")', 'req.header("X-Other-Signature")')
    for d in (a, b):
        with open(os.path.join(d, "source.py"), "a") as f:
            f.write('\nAPI = "https://api.github.com/repos"\n')
    c = run(b, "manifest")["manifest"]
    assert c.result == "fail" and any("calls api.github.com, the API host of bobbin 'acme.kitcode'" in m
                                      for m in messages(c)), messages(c)
    for d in (a, b):
        with open(os.path.join(d, "source.py"), "a") as f:
            f.write("from hookspool.providers import shared_provider  # noqa\n")
    assert run(b, "manifest")["manifest"].result == "pass"


def test_manifest_green_shared_documentation_links_in_comments_and_docstrings(tmp_path):
    a = copy(tmp_path, "kitcode")
    b = copy(tmp_path, "kitcode", "kitother")
    edit(b, "source.py", 'auth_headers = ("X-Kitcode-Signature",)', 'auth_headers = ("X-Other-Signature",)')
    edit(b, "source.py", 'req.header("X-Kitcode-Signature")', 'req.header("X-Other-Signature")')
    for d in (a, b):
        with open(os.path.join(d, "source.py"), "a") as f:
            f.write('\n# docs: https://docs.python.org/3/library/hmac.html\n\n\n'
                    'def helper():\n    """See https://docs.python.org/3/library/hmac.html"""\n')
        with open(os.path.join(d, "bobbin.toml"), "a") as f:
            f.write('\n# docs: https://docs.python.org/3/library/hmac.html\n')
    assert run(b, "manifest")["manifest"].result == "pass"
    for d in (a, b):   # a host in a string constant is a call, wherever the link sits in the file
        with open(os.path.join(d, "source.py"), "a") as f:
            f.write('\nAPI = "https://docs.python.org/3/library/hmac.html"\n')
    c = run(b, "manifest")["manifest"]
    assert c.result == "fail" and any("calls docs.python.org" in m for m in messages(c))


def test_manifest_green_is_static_and_needs_no_import(tmp_path):
    d = copy(tmp_path, "kitcode")
    edit(d, "source.py", '"""Kit fixture', 'raise SystemExit("never imported by the manifest check")\n"""Kit fixture')
    assert run(d, "manifest")["manifest"].result == "pass"


# ---- files and todos -----------------------------------------------------------------------------------------
def test_files_red_and_green(tmp_path):
    d = copy(tmp_path, "kitcode")
    assert run(d, "files")["files"].result == "pass"
    os.remove(os.path.join(d, "AGENTS.md"))
    shutil.rmtree(os.path.join(d, "captures"))
    c = run(d, "files")["files"]
    assert c.result == "fail" and "AGENTS.md" in c.detail and "captures/*.http" in c.detail


def test_todos_red_with_file_and_line(tmp_path):
    d = copy(tmp_path, "kitcode")
    assert run(d, "todos")["todos"].result == "pass"
    edit(d, "README.md", "Kit fixture:", "Kit fixture (TODO: say more):")
    c = run(d, "todos")["todos"]
    assert c.result == "fail" and c.detail.startswith("fill in the TODOs") and c.fix == "fill in the TODOs"
    assert [(f.file, f.line) for f in c.findings] == [("README.md", 9)]


# ---- core-only (14.3.7, exact) ------------------------------------------------------------------------------------
BANNED = [
    ("import hmac", "imports hmac"), ("from urllib import request", "imports urllib"),
    ("import urllib.request", "imports urllib.request"), ("import http.client", "imports http.client"),
    ("import requests", "imports requests"), ("import httpx", "imports httpx"), ("import aiohttp", "imports aiohttp"),
    ("import socket", "imports socket"), ("import ssl", "imports ssl"), ("import subprocess", "imports subprocess"),
    ("import asyncio", "imports asyncio"), ("import threading", "imports threading"),
    ("import multiprocessing", "imports multiprocessing"), ("import signal", "imports signal"),
    ("import sqlite3", "imports sqlite3"), ("import fcntl", "imports fcntl"), ("import tempfile", "imports tempfile"),
    ("import shutil", "imports shutil"), ("import pickle", "imports pickle"), ("import logging", "imports logging"),
    ("import importlib", "imports importlib"), ("from importlib import util", "imports importlib"),
    ("X = open('/tmp/x')", "calls open()"), ("print('x')", "calls print()"), ("eval('1')", "calls eval()"),
    ("exec('1')", "calls exec()"), ("compile('1', 'x', 'eval')", "calls compile()"), ("__import__('os')", "calls __import__()"),
    ("import os\nos.system('x')", "uses os.system"), ("import os\nX = os.environ", "uses os.environ"),
    ("import os\nX = os.getenv('X')", "uses os.getenv"), ("from os import environ", "imports os.environ"),
    ("import os as o\no.remove('x')", "uses os.remove"), ("import time\ntime.sleep(1)", "calls time.sleep"),
    ("from time import sleep", "imports time.sleep"), ("import sys\nsys.path.insert(0, 'x')", "uses sys.path"),
    ("from sys import path", "uses sys.path"), ("def f(p):\n    p.write_text('x')", "calls .write_text()"),
    ("def f(p):\n    p.write_bytes(b'x')", "calls .write_bytes()"), ("def f(p):\n    p.unlink()", "calls .unlink()"),
    ("def f(p):\n    return p.open()", "calls .open()"), ("import builtins\nbuiltins.open('x')", "builtins.open"),
    ("import hookspool.bobbins.other.source", "imports hookspool.bobbins.other.source"),
    ("from hookspool.bobbins.other import source", "imports hookspool.bobbins.other"),
    ("from hookspool import bobbins", "imports hookspool.bobbins"), ("from . import helpers", "relative import"),
    ("from ..other import source", "relative import"),
    # os and sys reached through a module that re-exports them
    ("import os.path as osp\nX = osp.os.environ['X']", "uses .environ through osp.os.environ"),
    ("import os.path as osp\nosp.os.system('x')", "uses .system through osp.os.system"),
    ("import os.path as osp\nY = osp.os.listdir('.')", "reaches os through osp.os"),
    ("import os.path\nX = os.path.os.environ['X']", "uses .environ through os.path.os.environ"),
    ("import os.path\nos.path.sys.path.insert(0, 'x')", "reaches sys through os.path.sys"),
    ("from os import path\nX = path.os.getenv('X')", "uses .getenv through path.os.getenv"),
    ("import glob\nX = glob.os.environ['X']", "uses .environ through glob.os.environ"),
    ("import glob\nX = glob.sys.modules", "reaches sys through glob.sys"),
    ("import shlex\nshlex.os.popen('x')", "uses .popen through shlex.os.popen"),
    ("def f(m):\n    return m.util.putenv('X', '1')", "uses .putenv through m.util.putenv"),
]


@pytest.mark.parametrize("snippet,needle", BANNED, ids=[b[1] for b in BANNED])
def test_core_only_red_for_every_banned_use(tmp_path, snippet, needle):
    d = copy(tmp_path, "kitcode")
    with open(os.path.join(d, "source.py")) as f:
        n = len(f.read().splitlines())
    with open(os.path.join(d, "source.py"), "a") as f:
        f.write("\n" + snippet + "\n")
    c = run(d, "core-only")["core-only"]
    assert c.result == "fail", snippet
    hits = [f for f in c.findings if needle in f.message]
    assert hits, messages(c)
    assert hits[0].file == "source.py" and hits[0].line > n, (hits[0].where(), n)


def test_core_only_green_for_what_core_allows(tmp_path):
    d = copy(tmp_path, "kitproc")
    with open(os.path.join(d, "processor.py"), "a") as f:
        f.write("\nimport os\nimport os.path\nimport os.path as osp\nfrom os import path as p\nimport time\n"
                "import json\nfrom hookspool.plugin import get_path\n"
                "X = os.path.join('a', 'b')\nY = osp.join('a', 'b')\nZ = p.basename(osp.dirname('a'))\n"
                "T = time.time\nJ = json.dumps\n")
    write(d, "tests/test_own.py", "import hmac\nimport socket\n")         # the bobbin's own tests are not bobbin code
    c = run(d, "core-only")["core-only"]
    assert c.result == "pass", messages(c)


def test_core_only_reports_a_re_export_once_and_leaves_plain_attributes_alone(tmp_path):
    d = copy(tmp_path, "kitcode")
    with open(os.path.join(d, "source.py"), "a") as f:
        f.write("\nimport glob\nX = glob.os.environ['X']\n"                     # one finding, not .os plus .environ
                "\n\ndef g(batch, ctx):\n    return batch.meta.system, ctx.settings['popen']\n")   # not a chain of .attr
    c = run(d, "core-only")["core-only"]
    assert len(c.findings) == 1, messages(c)                 # batch.meta.system and ctx.settings[...] are not os
    assert c.findings[0].message == ("uses .environ through glob.os.environ: settings are ctx.settings and secrets"
                                     " ctx.secret(); a bobbin never reads the environment")


def test_core_only_reports_a_syntax_error(tmp_path):
    d = copy(tmp_path, "kitcode")
    write(d, "extra.py", "def broken(:\n")
    c = run(d, "core-only")["core-only"]
    assert c.result == "fail" and c.findings[0].where() == "extra.py:1" and "does not parse" in c.findings[0].message


def test_ban_findings_unit():
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write("import os.path\nimport time as t\nx = os.path.join('a')\nt.sleep(1)\n")
    try:
        assert [(x.line, x.message.split(":")[0]) for x in K.ban_findings(f.name, "m.py")] == [(4, "calls time.sleep")]
    finally:
        os.unlink(f.name)


# ---- docs (14.3.8) -------------------------------------------------------------------------------------------------
def test_docs_red_a_secret_or_required_setting_not_named(tmp_path):
    d = copy(tmp_path, "kitproc")
    edit(d, "AGENTS.md", "`owner`", "the account")
    assert run(d, "docs")["docs"].result == "pass", "the table still names it (settings = { owner = ... })"
    edit(d, "AGENTS.md", 'settings = { owner = "owner@example.com" }\n', "")
    c = run(d, "docs")["docs"]
    assert any(f.file == "AGENTS.md" and f.message == "required setting `owner` is not named" for f in c.findings), \
        messages(c)
    assert any("does not expand" in m and "owner" in m for m in messages(c))


@pytest.mark.parametrize("old,new,needle", [
    ("## Needs from the human\n", "## Before you start\n", "no \"Needs from the human\" list"),
    ("- an account at the provider\n- the values of `secret`, written to their files by the human\n", "\n",
     "the \"Needs from the human\" list is empty"),
    ("(`outward`)", "", "no step is marked `outward`"),
    ("1. **Add", "**Add", None),
])
def test_docs_red_agents_structure(tmp_path, old, new, needle):
    d = copy(tmp_path, "kitcode")
    edit(d, "AGENTS.md", old, new)
    if needle is None:                                         # the numbered steps
        edit(d, "AGENTS.md", "2. **Register", "**Register")
        edit(d, "AGENTS.md", "3. **Verify", "**Verify")
        needle = "no numbered steps"
    c = run(d, "docs")["docs"]
    assert c.result == "fail" and any(needle in m for m in messages(c)), messages(c)


def test_docs_red_a_provider_changing_command_not_marked_outward(tmp_path):
    d = copy(tmp_path, "kitcode")
    with open(os.path.join(d, "AGENTS.md"), "a") as f:
        f.write("4. **Subscribe.** Run:\n\n   ```sh\n   curl -X POST https://api.example.com/v1/webhooks -d @hook.json\n"
                "   ```\n\n   Check: it answers 201.\n")
    c = run(d, "docs")["docs"]
    f, = [f for f in c.findings if "step 4" in f.message]
    assert f.file == "AGENTS.md" and f.line == 29 and "not marked `outward`" in f.message
    edit(d, "AGENTS.md", "4. **Subscribe.**", "4. **Subscribe** (`outward`).")
    assert run(d, "docs")["docs"].result == "pass"


def test_docs_green_a_local_command_needs_no_outward(tmp_path):
    d = copy(tmp_path, "kitcode")
    with open(os.path.join(d, "AGENTS.md"), "a") as f:
        f.write("4. **Probe the intake.** `curl -X POST http://127.0.0.1:8650/hooks/kitcode -d '{}'` answers 401.\n")
    assert run(d, "docs")["docs"].result == "pass"


@pytest.mark.parametrize("doc,old,new,needle", [
    ("SETUP.md", 'outputs = { "*" = "hermes:kitcode" }', "outputs = <OUTPUTS>", "not valid TOML"),
    ("SETUP.md", 'outputs = { "*" = "hermes:kitcode" }', 'settings = { nope = "x" }\noutputs = { "*" = "hermes:k" }',
     "does not expand: bobbins.kitcode.settings.nope: bobbin 'acme.kitcode' has no such setting"),
    ("AGENTS.md", "   ```toml\n", "   ```\n", "no ```toml block"),
    ("SETUP.md", 'bobbin = "acme.kitcode"\n', 'bobbin = "acme.other"\n', "has no instance of bobbin"),
    ("SETUP.md", 'bobbin = "acme.kitcode"\n', "", 'the [bobbins.<instance>] table names it: bobbin = "acme.kitcode"'),
])
def test_docs_red_the_config_table(tmp_path, doc, old, new, needle):
    d = copy(tmp_path, "kitcode")
    edit(d, doc, old, new)
    c = run(d, "docs")["docs"]
    assert c.result == "fail" and any(needle in m and f.file == doc for f in c.findings for m in [f.message]), \
        messages(c)


def test_docs_table_line_points_at_the_block(tmp_path):
    d = copy(tmp_path, "kitcode")
    edit(d, "SETUP.md", 'outputs = { "*" = "hermes:kitcode" }', "outputs = <OUTPUTS>")
    c = run(d, "docs")["docs"]
    f, = c.findings
    text = open(os.path.join(d, "SETUP.md")).read().splitlines()
    assert text[f.line - 2].startswith("```toml") and text[f.line - 1].startswith("[bobbins.kitcode]")


# ---- captures -------------------------------------------------------------------------------------------------------
def test_captures_red_without_a_rejected_capture(tmp_path):
    d = copy(tmp_path, "kitcode")
    os.remove(os.path.join(d, "captures", "bad_signature.http"))
    c = run(d, "captures")["captures"]
    assert c.result == "fail" and any("no rejected capture" in m for m in messages(c))


def test_captures_red_when_an_expect_does_not_hold(tmp_path):
    d = copy(tmp_path, "kitcode")
    edit(d, "captures/bad_signature.http", "# expect: unverified", "# expect: queued")
    c = run(d, "captures")["captures"]
    assert c.result == "fail"
    f = next(f for f in c.findings if "expected" in f.message)
    assert f.where() == "captures/bad_signature.http:1" and f.message == "expected queued, got unverified"


def test_captures_red_without_an_expect_line(tmp_path):
    d = copy(tmp_path, "kitcode")
    edit(d, "captures/event.http", "# expect: queued\n", "")
    c = run(d, "captures")["captures"]
    assert any("`# expect:` must be one of" in m for m in messages(c)) and any("no accepted capture" in m
                                                                                for m in messages(c))


@pytest.mark.parametrize("value,kind", [
    ("AKIA" + "ABCDEFGHIJKLMNOP", "AWS access key id"),
    ("someone@" + "realcorp.com", "e-mail address"),  # hookspool-scan: allow
    ("+1 415 " + "867 5309", "phone number"),
    ("8.8." + "4.4", "IP address"),  # hookspool-scan: allow
])
def test_captures_red_for_secrets_and_personal_data(tmp_path, value, kind):
    d = copy(tmp_path, "kitcode")
    edit(d, "captures/event.http", '"note":"PAYLOAD-SENTINEL-7f3a"', f'"note":"{value}"')
    c = run(d, "captures")["captures"]
    hits = [f for f in c.findings if f.message.startswith(kind)]
    assert hits and hits[0].file == "captures/event.http" and hits[0].line > 1, messages(c)
    assert value not in " ".join(messages(c)) or kind == "IP address", "a finding never echoes the value"


def test_personal_data_patterns_pass_reserved_values():
    ok = ("owner@example.com ops@sub.example.org a@b.test 555-0100 +1 555 0123 2026-10-23T10:00:00Z "
          "1800000000 1799999999000 192.0.2.1 10.0.0.1 v1.2.3 sha256=0b163d66eca9")
    assert S.personal_findings(ok) == []
    line = "call (415) 555-2671 or mail a@realcorp.com"  # hookspool-scan: allow
    assert [k for _, k, _ in S.personal_findings(line)] == ["e-mail address", "phone number"]


def test_captures_red_when_a_kit_secret_does_not_say_test(tmp_path):
    d = copy(tmp_path, "kitcode")
    edit(d, "captures/kit.toml", 'secret = "test-secret-kitcode"', 'secret = "hunter2-kitcode"')
    c = run(d, "captures")["captures"]
    assert any("must say it is a test value" in m for m in messages(c))


def test_captures_red_for_an_unknown_kit_key(tmp_path):
    d = copy(tmp_path, "kitcode")
    edit(d, "captures/kit.toml", "[secrets]", "bogus = 1\n[secrets]")
    c = run(d, "captures")["captures"]
    assert any(f.file == "captures/kit.toml" and "bogus: unknown key" in f.message for f in c.findings)


# ---- source kit ---------------------------------------------------------------------------------------------------------
def _kit_bobbin(tmp_path, name: str, plugin: str, secret: str, *, constant_callback: bool = False) -> str:
    d = os.path.join(str(tmp_path), "bobbins", VENDOR, name)
    os.makedirs(os.path.join(d, "captures"))
    src = open(os.path.join(ROOT, "tests", "kitplugins", f"{plugin}.py")).read()
    if constant_callback:
        src = src.replace('str(req.config.get("callback_url") or "")', repr(kits.CALLBACK_URL))
    write(d, "source.py", src)
    for f in os.listdir(os.path.join(ROOT, "tests", "captures", name)):
        shutil.copy(os.path.join(ROOT, "tests", "captures", name, f), os.path.join(d, "captures", f))
    write(d, "bobbin.toml", f'name = "{VENDOR}.{name}"\nversion = "1"\nsummary = "kit"\n[source]\nmodule = "source"\n'
                            '[secrets.secret]\ndescription = "s"\n[outputs."*"]\ndescription = "all"\n')
    write(d, "captures/kit.toml", f'[secrets]\nsecret = "{secret}"\n')
    return d


def test_source_kit_green_timestamps_and_a_verified_handshake(tmp_path):
    d = _kit_bobbin(tmp_path, "kitstamped", "stamped", kits.STAMPED_SECRET)
    c = run(d, "source-kit")["source-kit"]
    assert c.result == "pass", messages(c)
    assert "15 passed, 1 not applicable" in c.detail          # stale, future and both handshake cases ran


def test_source_kit_green_an_unauthenticated_constant_handshake(tmp_path):
    d = _kit_bobbin(tmp_path, "kithead", "headonly", kits.HEAD_SECRET, constant_callback=True)
    cs = run(d, "source-kit", "captures")
    assert cs["captures"].result == "fail" and any("no rejected capture" in m for m in messages(cs["captures"]))
    c = cs["source-kit"]
    assert c.result == "pass", messages(c)
    assert "not applicable" in c.detail and "unauthenticated" not in c.detail


def test_source_kit_red_a_source_that_accepts_anything(tmp_path):
    d = copy(tmp_path, "kitcode")
    edit(d, "source.py", 'return v.hmac_prefixed(req.header("X-Kitcode-Signature"), "sha256=", secrets["secret"], '
                         "req.body)", "return True")
    c = run(d, "source-kit")["source-kit"]
    assert c.result == "fail"
    cases = {m.split(":")[0] for m in messages(c)}
    assert {"unsigned", "tampered"} <= cases, messages(c)
    assert all(f.file == "captures/event.http" for f in c.findings)


def test_source_kit_red_a_static_token_without_body_signed_false(tmp_path):
    d = copy(tmp_path, "kitproc")
    edit(d, "captures/kit.toml", "body_signed = false            # a static token does not cover the body\n", "")
    c = run(d, "source-kit")["source-kit"]
    assert c.result == "fail"
    f, = c.findings
    assert f.message.startswith("tampered:") and "body_signed = false" in f.message


def test_source_kit_red_a_source_that_reaches_the_network(tmp_path):
    d = copy(tmp_path, "kitcode")
    edit(d, "source.py", "def verify(req: Request, secrets: Secrets) -> bool:\n",
         "def verify(req: Request, secrets: Secrets) -> bool:\n    import socket\n    try:\n"
         "        socket.create_connection(('192.0.2.1', 9), timeout=1)\n    except OSError:\n        pass\n")
    c = run(d, "source-kit")["source-kit"]
    assert c.result == "fail" and any("opened network connection(s) to 192.0.2.1" in m for m in messages(c))


def test_source_kit_red_a_source_that_does_not_load(tmp_path):
    d = copy(tmp_path, "kitcode")
    edit(d, "source.py", "from hookspool.plugin", "from hookspool.nowhere")
    c = run(d, "source-kit")["source-kit"]
    assert c.result == "fail" and "does not load (ModuleNotFoundError" in c.detail


# ---- processor -------------------------------------------------------------------------------------------------------------
def test_processor_red_an_undeclared_output(tmp_path):
    d = copy(tmp_path, "kitproc")
    edit(d, "processor.py", '"event_type": "changed"', '"event_type": "other"')
    c = run(d, "processor")["processor"]
    assert c.result == "fail" and any(f.file == "bobbin.toml" and "other" in f.message and "not declared" in f.message
                                      for f in c.findings), messages(c)


def test_processor_red_swallowing_a_provider_failure(tmp_path):
    d = copy(tmp_path, "kitproc")
    edit(d, "processor.py", '    data = ctx.http.get(', '    try:\n        data = ctx.http.get(')
    edit(d, "processor.py", 'label="items")\n', 'label="items")\n    except Exception:\n        return []\n')
    c = run(d, "processor")["processor"]
    assert c.result == "fail"
    assert any("when ctx.http raised Retryable the batch exited 0, not 75" in m for m in messages(c)), messages(c)
    assert any("when ctx.http raised Permanent the batch exited 0, not 65" in m for m in messages(c))


def test_processor_red_a_network_connection(tmp_path):
    d = copy(tmp_path, "kitproc")
    edit(d, "processor.py", "    out = []\n", "    import socket\n    try:\n"
         "        socket.create_connection(('192.0.2.1', 9), timeout=1)\n    except OSError:\n        pass\n"
         "    out = []\n")
    c = run(d, "processor")["processor"]
    assert c.result == "fail" and any("opened a network connection (to 192.0.2.1)" in m for m in messages(c))


def test_processor_red_a_loopback_connection_too(tmp_path):
    d = copy(tmp_path, "kitproc")
    edit(d, "processor.py", "    out = []\n", "    import socket\n    try:\n"
         "        socket.create_connection(('127.0.0.1', 9), timeout=1)\n    except OSError:\n        pass\n"
         "    out = []\n")
    c = run(d, "processor")["processor"]
    assert c.result == "fail" and any("opened a network connection (to 127.0.0.1)" in m for m in messages(c)), messages(c)


def test_netblock_loopback_flag_and_udp():
    import socket
    from hookspool.testing.netblock import NetworkBlocked, block_network
    with block_network() as attempts:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        with pytest.raises(NetworkBlocked):
            s.sendto(b"x", ("192.0.2.1", 9))
        s.close()
        assert socket.getaddrinfo("127.0.0.1", 9)                  # loopback stays usable by default
    assert attempts == ["192.0.2.1"]
    with block_network(loopback=False) as attempts:
        with pytest.raises(NetworkBlocked):
            socket.create_connection(("127.0.0.1", 9), timeout=1)
        with pytest.raises(NetworkBlocked):
            socket.getaddrinfo("localhost", 9)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        with pytest.raises(NetworkBlocked):
            s.sendto(b"x", ("127.0.0.1", 9))
        s.close()
    assert attempts == ["127.0.0.1", "localhost", "127.0.0.1"]
    assert socket.getaddrinfo("127.0.0.1", 9)                      # restored on exit


def test_processor_red_different_emits_for_the_same_batch(tmp_path):
    d = copy(tmp_path, "kitproc")
    edit(d, "bobbin.toml", "state = true", "state = false")
    edit(d, "processor.py", '    seen = set(ctx.load_state("seen", []))\n', "    import os\n    seen = set()\n")
    edit(d, "processor.py", '    ctx.save_state("seen", sorted(seen))\n', "")
    edit(d, "processor.py", '"owner": ctx.settings["owner"]', '"pid": os.getpid()')
    c = run(d, "processor")["processor"]
    assert c.result == "fail" and any("printed different emits" in m for m in messages(c)), messages(c)


def test_processor_red_a_crash_names_the_exit_and_the_class(tmp_path):
    d = copy(tmp_path, "kitproc")
    edit(d, "processor.py", 'item["id"]', 'item["missing"]', count=0)
    c = run(d, "processor")["processor"]
    f, = c.findings
    assert f.file == "captures/event.http" and "process exited 75" in f.message and "KeyError" in f.message


def test_processor_skipped_without_one(tmp_path):
    assert run(os.path.join(FIX, "kitcode"), "processor")["processor"].result == "skip"


def test_non_emit_lines():
    good = '{"enqueue":{"source":"s"}}\n\n{"enqueue": {"key": "k"}}\n'
    assert K._non_emit_lines(good) == []
    assert K._non_emit_lines('hello\n{"enqueue":{}}\n{"other":1}\n[1]\n{"enqueue":1}\n') == [1, 3, 4, 5]


# ---- tests ----------------------------------------------------------------------------------------------------------------
def test_tests_green_red_and_network(tmp_path):
    d = copy(tmp_path, "kitcode")
    assert run(d, "tests")["tests"].result == "skip"
    write(d, "tests/test_kitcode_own.py", "def test_ok():\n    assert 1 + 1 == 2\n")
    c = run(d, "tests")["tests"]
    assert c.result == "pass" and "1 passed" in c.detail
    write(d, "tests/test_kitcode_bad.py", "def test_bad():\n    assert 1 + 1 == 3\n")
    c = run(d, "tests")["tests"]
    assert c.result == "fail" and any("test_bad" in m for m in messages(c)), messages(c)
    os.remove(os.path.join(d, "tests", "test_kitcode_bad.py"))
    write(d, "tests/test_kitcode_net.py", "import socket\n\n\ndef test_net():\n    try:\n"
                                         "        socket.create_connection(('192.0.2.1', 9), timeout=1)\n"
                                         "    except OSError:\n        pass\n")
    c = run(d, "tests")["tests"]
    assert c.result == "fail" and any("tried to reach the network" in m for m in messages(c)), (c.detail, messages(c))


def test_tests_run_isolated_with_a_scrubbed_environment(tmp_path, monkeypatch):
    """`bobbin check` runs on pull request code: its tests see PATH, LANG, TMPDIR and a throwaway HOME, never the
    caller's tokens or HOOKSPOOL_* settings, and run under -I (no PYTHON* variables, no user site, no working
    directory on sys.path)."""
    monkeypatch.setenv("GH_TOKEN", "test-token-must-not-leak")
    monkeypatch.setenv("HOOKSPOOL_SECRET_REFS", '{"x": {"file": "/nowhere"}}')
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "planted"))
    monkeypatch.setenv("PYTHONINSPECT", "1")
    d = copy(tmp_path, "kitcode")
    write(d, "tests/test_kitcode_env.py", f"""import os
import sys


def test_scrubbed():
    allowed = {{"PATH", "HOME", "LANG", "TMPDIR",
               "LC_CTYPE", "__CF_USER_TEXT_ENCODING"}}       # set by Python's locale coercion and by macOS
    extra = {{k for k in os.environ if k not in allowed and not k.startswith("PYTEST_")}}
    assert not extra, sorted(extra)
    assert os.environ["HOME"] != {os.path.expanduser("~")!r}
    assert sys.flags.isolated == 1 and sys.flags.ignore_environment == 1
    assert {str(tmp_path / "planted")!r} not in sys.path
""")
    c = run(d, "tests")["tests"]
    assert c.result == "pass" and "1 passed" in c.detail, (c.detail, messages(c))


# ---- scope (advisory) ----------------------------------------------------------------------------------------------------
def _git(root: str, *args: str) -> str:
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.test", "PATH": os.environ["PATH"], "HOME": root}
    return subprocess.run(["git", "-C", root, *args], check=True, env=env, capture_output=True, text=True).stdout


def test_scope_bobbin_only_or_touches_core(tmp_path):
    root = str(tmp_path / "repo")
    d = os.path.join(root, "bobbins", VENDOR, "kitcode")
    shutil.copytree(os.path.join(FIX, "kitcode"), d)
    write(root, "core.py", "x = 1\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD").strip()
    assert run(d, "scope")["scope"].result == "skip"                     # no --base
    edit(d, "README.md", "Kit fixture", "A kit fixture")
    c = run(d, "scope", base=base)["scope"]
    assert c.result == "pass" and c.detail.startswith("bobbin-only") and not c.required
    write(root, "core.py", "x = 2\n")
    card = K.check_bobbin(d, only=["scope"], base=base)
    c = card.checks[0]
    assert c.result == "fail" and c.detail.startswith("touches core") and [f.file for f in c.findings] == ["core.py"]
    assert card.ok and card.scope == "touches-core", "advisory: does not fail the card"
    strict = K.check_bobbin(d, only=["scope"], base=base, strict=True)
    assert not strict.ok and "advisory failing under --strict: scope" in strict.summary()


def test_scope_a_new_bobbin_made_by_the_guide_is_bobbin_only(tmp_path):
    """A new built-in bobbin as docs/bobbins/writing-a-bobbin.md makes it: the directory and its own test file.
    The README catalogue and the changelog are not part of it, and touching them is a change outside the bobbin."""
    root = str(tmp_path / "repo")
    write(root, "README.md", "# repo\n")
    write(root, "CHANGELOG.md", "# changes\n")
    write(root, "hookspool/bobbins/__init__.py", "")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD").strip()
    d = os.path.join(root, "hookspool", "bobbins", VENDOR, "kitcode")
    shutil.copytree(os.path.join(FIX, "kitcode"), d)
    write(root, f"hookspool/bobbins/{VENDOR}/__init__.py", "")          # a new vendor's empty package file
    write(root, "tests/bobbins/test_acme_kitcode.py", "def test_ok():\n    pass\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "a new bobbin")
    c = run(d, "scope", base=base)["scope"]
    assert c.result == "pass" and c.detail.startswith("bobbin-only"), c.detail
    write(root, "docs/a page with spaces.md", "x\n")              # untracked; -z keeps the name whole
    edit(root, "CHANGELOG.md", "# changes", "# changes\n- kitcode")
    c = run(d, "scope", base=base)["scope"]
    assert c.result == "fail" and sorted(f.file for f in c.findings) == ["CHANGELOG.md", "docs/a page with spaces.md"]


# ---- the scorecard and the command --------------------------------------------------------------------------------------
def test_json_report_schema(capsys, tmp_path):
    d = copy(tmp_path, "kitcode")
    edit(d, "README.md", "Kit fixture:", "TODO Kit fixture:")
    rc = cli(["bobbin", "check", d, "--report", "json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert set(doc) == {"schema", "bobbin", "dir", "ok", "strict", "scope", "summary", "checks"}
    assert doc["schema"] == K.SCHEMA == "hookspool.bobbin-check/1"
    assert doc["bobbin"] == "acme.kitcode" and doc["ok"] is False and doc["strict"] is False and doc["scope"] is None
    assert isinstance(doc["dir"], str) and doc["summary"].startswith("bobbin check acme.kitcode: FAIL, 1 of 11")
    assert [c["name"] for c in doc["checks"]] == K.CHECK_NAMES
    for c in doc["checks"]:
        assert set(c) == {"name", "required", "result", "detail", "fix", "findings"}
        assert c["result"] in ("pass", "fail", "skip") and isinstance(c["required"], bool)
        assert isinstance(c["detail"], str) and isinstance(c["fix"], str)
        assert bool(c["fix"]) == (c["result"] == "fail")
        for f in c["findings"]:
            assert set(f) == {"file", "line", "message"} and isinstance(f["line"], int)
    todos = next(c for c in doc["checks"] if c["name"] == "todos")
    assert todos["findings"] == [{"file": "README.md", "line": 9, "message": "TODO placeholder"}]
    assert [c["required"] for c in doc["checks"]] == [True] * 11 + [False, False]


def test_markdown_and_text_reports(capsys):
    d = os.path.join(FIX, "kitcode")
    assert cli(["bobbin", "check", d, "--report", "md"]) == 0
    md = capsys.readouterr().out
    assert md.startswith("### Bobbin `acme.kitcode`: PASS") and "| Check | Result | Detail |" in md
    assert re.search(r"^\| source-kit \| pass \| 16 cases", md, re.M) and "| scope (advisory) | skip |" in md
    assert cli(["bobbin", "check", d]) == 0
    text = capsys.readouterr().out
    assert text.splitlines()[0] == "bobbin check acme.kitcode: PASS, 9 of 11 required checks pass, 2 not applicable"


def test_markdown_lists_findings_and_how_to_fix(tmp_path):
    d = copy(tmp_path, "kitcode")
    edit(d, "README.md", "Kit fixture:", "TODO Kit fixture:")
    md = K.render_md(K.check_bobbin(d, only=["todos"]).as_dict())
    assert "| todos | **FAIL** | fill in the TODOs (1 left). How to fix: fill in the TODOs |" in md
    assert "- todos: `README.md:9` TODO placeholder" in md


def test_the_command_refuses_a_directory_that_is_not_a_bobbin(tmp_path, capsys):
    assert cli(["bobbin", "check", str(tmp_path)]) == 2
    assert "is not a bobbin directory" in capsys.readouterr().err
    assert cli(["bobbin", "check"]) == 1


def test_the_command_needs_no_config(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "broken.toml"
    bad.write_text("[[[ not toml")
    monkeypatch.setenv("HOOKSPOOL_CONFIG", str(bad))
    assert cli(["bobbin", "check", os.path.join(FIX, "kittpl")]) == 0


# ---- bobbin new passes, except for its TODOs ------------------------------------------------------------------------------
def fill_in(d: str) -> None:
    """What the author of a scaffold does: write the TODOs, and the provider's real pages and names."""
    for base, _dirs, files in os.walk(d):
        for f in files:
            p = os.path.join(base, f)
            text = re.sub(r"\bTODO\b:? ?", "", open(p).read())
            if f == "bobbin.toml":
                for k, v in PROVIDER.items():
                    text = re.sub(rf'^{k} = ".*"$', f'{k} = "{v}"', text, flags=re.M)
            elif f in K.FRONTMATTER_DOCS:
                text = re.sub(r"^provider: .*$", f"provider: {PROVIDER['vendor']} {PROVIDER['product']}", text, count=1, flags=re.M)
                text = re.sub(r"^webhook_docs: .*$", f"webhook_docs: {PROVIDER['webhook_docs']}", text, count=1, flags=re.M)
            open(p, "w").write(text)


PROVIDER = {"vendor": "Acme", "product": "Hooks", "homepage": "https://github.com",
            "webhook_docs": "https://docs.github.com/en/webhooks",
            "signature_docs": "https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries",
            "api_docs": "https://docs.github.com/en/rest"}


@pytest.mark.parametrize("flags", [[], ["--code"], ["--processor"], ["--set", "scheme=url-token", "--set", "header="],
                                   ["--set", "scheme=hmac-sha256-base64", "--set", "prefix="],
                                   ["--set", "scheme=shared-secret-header"], ["--set", "scheme=hmac-sha1-base64-url"]],
                         ids=["webhook", "code", "processor", "url-token", "base64", "shared-secret", "sha1-url"])
def test_bobbin_new_passes_every_check_but_the_todos(tmp_path, capsys, flags):
    assert cli(["bobbin", "new", "acme.hooks", "--dir", str(tmp_path), *flags]) == 0
    capsys.readouterr()
    d = os.path.join(str(tmp_path), "acme", "hooks")
    card = K.check_bobbin(d)
    failed = [c.name for c in card.checks if c.result == "fail"]
    assert failed == ["provider", "todos"], K.render_text(card.as_dict())
    assert card.summary() == "bobbin check acme.hooks: FAIL, 2 of 11 required checks fail (provider, todos)"
    assert next(c for c in card.checks if c.name == "todos").detail.startswith("fill in the TODOs")
    prov = next(c for c in card.checks if c.name == "provider")
    f, = prov.findings                                        # the placeholders are one problem, with one message
    assert f.file == "bobbin.toml" and "fill in [provider]" in f.message and "README.md, SETUP.md and AGENTS.md" in f.message
    for base, _dirs, files in os.walk(d):
        for f in files:
            assert f.endswith((".toml", ".md", ".py", ".http")), f"the kit wrote {f} into the bobbin"
    fill_in(d)                                                # filling them in is all that is left
    assert K.check_bobbin(d).ok, K.render_text(K.check_bobbin(d).as_dict())


def test_the_webhook_bobbin_passes_the_conformance_kit():
    card = K.check_bobbin(os.path.join(ROOT, "hookspool", "bobbins", "hookspool", "webhook"))
    assert card.ok, K.render_text(card.as_dict())
    assert [c.result for c in card.checks if c.name in ("captures", "source-kit", "docs")] == ["pass"] * 3


def test_bobbin_new_with_frozen_settings_passes_every_check_but_the_todos(tmp_path, capsys):
    assert cli(["bobbin", "new", "acme.hooks", "--dir", str(tmp_path), "--set", 'key=["id", "type"]',
                "--set", "dedup=header:X-Delivery-Id", "--set", 'ignore=["type=ping"]']) == 0
    capsys.readouterr()
    d = os.path.join(str(tmp_path), "acme", "hooks")
    card = K.check_bobbin(d)
    assert [c.name for c in card.checks if c.result == "fail"] == ["provider", "todos"], K.render_text(card.as_dict())
    fill_in(d)
    assert K.check_bobbin(d).ok
    with open(os.path.join(d, "bobbin.toml"), "rb") as f:
        tpl = tomllib.load(f)["source"]["template"]
    assert tpl["key"] == ["id", "type"] and tpl["dedup"] == "header:X-Delivery-Id" and tpl["ignore"] == ["type=ping"]


def test_the_default_test_secret_is_the_scaffolds():
    assert K.DEFAULT_TEST_SECRET == BL.SCAFFOLD_SECRET and "test" in K.DEFAULT_TEST_SECRET


# ---- name (14.3.1) ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name,needle", [
    ("kitcode", "has no vendor"), ("acme.kit.code", "has 2 dots"), ("Acme.kitcode", "must be <vendor>.<product>"),
    ("acme.kit_code", "must be <vendor>.<product>"), ("acme." + "k" * 41, "must be <vendor>.<product>"),
])
def test_name_red_for_a_name_that_is_not_vendor_dot_product(tmp_path, name, needle):
    d = copy(tmp_path, "kitcode")
    edit(d, "bobbin.toml", 'name = "acme.kitcode"', f'name = "{name}"')
    c = run(d, "name")["name"]
    assert c.result == "fail" and c.required
    f, = c.findings
    assert f.file == "bobbin.toml" and f.line == 1 and needle in f.message, f.message


def test_name_red_for_the_wrong_folder(tmp_path):
    d = os.path.join(str(tmp_path), "bobbins", "other", "kitcode")
    shutil.copytree(os.path.join(FIX, "kitcode"), d)
    c = run(d, "name")["name"]
    assert c.result == "fail" and any("must live in acme/kitcode/" in m and "not other/kitcode/" in m for m in messages(c))


def test_name_red_for_the_hookspool_namespace_outside_the_repository(tmp_path):
    d = os.path.join(str(tmp_path), "bobbins", "hookspool", "kitcode")
    shutil.copytree(os.path.join(FIX, "kitcode"), d)
    edit(d, "bobbin.toml", 'name = "acme.kitcode"', 'name = "hookspool.kitcode"')
    c = run(d, "name")["name"]
    assert c.result == "fail" and any("hookspool. namespace is reserved" in m for m in messages(c)), messages(c)


def test_name_red_for_a_private_bobbin_that_takes_a_built_ins_name(tmp_path):
    d = os.path.join(str(tmp_path), "bobbins", "github", "repo")
    shutil.copytree(os.path.join(FIX, "kitcode"), d)
    edit(d, "bobbin.toml", 'name = "acme.kitcode"', 'name = "github.repo"')
    c = run(d, "name")["name"]
    assert c.result == "fail" and any("'github.repo' is a built-in bobbin" in m for m in messages(c)), messages(c)


def test_name_green_for_the_fixture_and_a_dash_written_as_underscore(tmp_path):
    assert run(os.path.join(FIX, "kitcode"), "name")["name"].result == "pass"
    d = os.path.join(str(tmp_path), "bobbins", "my_co", "kit_code")
    shutil.copytree(os.path.join(FIX, "kitcode"), d)
    edit(d, "bobbin.toml", 'name = "acme.kitcode"', 'name = "my-co.kit-code"')
    assert run(d, "name")["name"].result == "pass"


# ---- provider and frontmatter ----------------------------------------------------------------------------------------
WEBHOOK_DOCS = 'webhook_docs = "https://docs.github.com/en/webhooks"\n'


@pytest.mark.parametrize("old,new,needle", [
    (WEBHOOK_DOCS, "", "provider.webhook_docs: required"),
    ('homepage = "https://github.com"', 'homepage = "http://github.com"', "must be https"),
    ('homepage = "https://github.com"', 'homepage = "https://example.com/acme"', "example or local host"),
    ('homepage = "https://github.com"', 'homepage = "https://docs.acme.test/"', "example or local host"),
    ('homepage = "https://github.com"', 'homepage = "https://localhost/x"', "has no public host name"),
    ('homepage = "https://github.com"', 'homepage = "https://192.0.2.10/docs"', "is an IP address"),
    ('homepage = "https://github.com"', 'homepage = "https://<PROVIDER_HOST>/docs"', "is a placeholder"),
    ('homepage = "https://github.com"', 'homepage = "https://github.com/..."', "is a placeholder"),
    ('signature_docs = "https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries"\n', "",
     "provider.signature_docs: required (the bobbin declares a secret"),
    ('vendor = "Acme"\n', "", "provider.vendor: required"),
    ('product = "Kitcode"', 'product = "<PRODUCT>"', "provider.product: '<PRODUCT>' is a placeholder"),
    ('homepage = "https://github.com"', 'homepage = "https://github.com"\nmore = "x"', "provider.more: unknown key"),
])
def test_provider_red_for_each_rule(tmp_path, old, new, needle):
    d = copy(tmp_path, "kitcode")
    edit(d, "bobbin.toml", old, new)
    c = run(d, "provider")["provider"]
    assert c.result == "fail" and c.required, c.detail
    assert any(needle in m and f.file == "bobbin.toml" for f in c.findings for m in [f.message]), messages(c)


def test_provider_red_without_the_table(tmp_path):
    d = copy(tmp_path, "kitcode")
    p = os.path.join(d, "bobbin.toml")
    text = open(p).read()
    open(p, "w").write(re.sub(r"\[provider\]\n(?:[a-z_]+ = .*\n)+", "", text))
    c = run(d, "provider")["provider"]
    assert c.result == "fail" and messages(c) == ["[provider]: required: vendor, product, homepage and webhook_docs (the"
                                                  " provider's developer page for these webhooks)"]


def test_provider_red_api_docs_for_a_processor_that_calls_the_api(tmp_path):
    d = copy(tmp_path, "kitproc")
    edit(d, "bobbin.toml", 'api_docs = "https://docs.github.com/en/rest"\n', "")
    c = run(d, "provider")["provider"]
    assert c.result == "fail" and any("provider.api_docs: required (the processor calls" in m for m in messages(c))
    assert K.processor_calls_api(K.Bobbin(os.path.join(FIX, "kitproc")))
    assert not K.processor_calls_api(K.Bobbin(os.path.join(FIX, "kitcode")))


@pytest.mark.parametrize("doc", K.FRONTMATTER_DOCS)
@pytest.mark.parametrize("old,new,needle", [
    ("bobbin: acme.kitcode", "bobbin: acme.other", "frontmatter: bobbin: 'acme.other' does not match"),
    ("provider: Acme Kitcode", "provider: Acme", "frontmatter: provider: 'Acme' does not match"),
    ("webhook_docs: https://docs.github.com/en/webhooks", "webhook_docs: https://docs.github.com/en",
     "frontmatter: webhook_docs: 'https://docs.github.com/en' does not match"),
    ("webhook_docs: https://docs.github.com/en/webhooks\n", "", "frontmatter: webhook_docs: missing"),
    ("---\nbobbin:", "bobbin:", "does not open with --- frontmatter"),
])
def test_provider_red_a_frontmatter_mismatch_in_each_document(tmp_path, doc, old, new, needle):
    d = copy(tmp_path, "kitcode")
    edit(d, doc, old, new)
    c = run(d, "provider")["provider"]
    assert c.result == "fail" and [f.file for f in c.findings] == [doc], messages(c)
    assert needle in c.findings[0].message, messages(c)


def test_provider_only_agents_md_may_add_frontmatter_keys(tmp_path):
    d = copy(tmp_path, "kitcode")
    edit(d, "AGENTS.md", "---\n\n", "description: set up acme.kitcode\n---\n\n")
    assert run(d, "provider")["provider"].result == "pass"
    edit(d, "SETUP.md", "---\n\n", "description: set up acme.kitcode\n---\n\n")
    c = run(d, "provider")["provider"]
    assert c.result == "fail" and any(f.file == "SETUP.md" and "unknown key(s) description" in f.message for f in c.findings)


def test_provider_green_for_the_fixtures_and_quoted_frontmatter(tmp_path):
    for name in FIXTURES:
        c = run(os.path.join(FIX, name), "provider")["provider"]
        assert c.result == "pass", (name, messages(c))
    d = copy(tmp_path, "kitcode")
    edit(d, "README.md", "provider: Acme Kitcode", 'provider: "Acme Kitcode"')
    assert run(d, "provider")["provider"].result == "pass"


def test_url_problem_unit():
    ok = ("https://developers.google.com/workspace/gmail/api/guides/push", "https://kaneo.app/docs#signature-verification",
          *(f"https://{h}/docs" for h in ("cafe" + ".ca", "abc" + ".de", "deca" + ".de")))
    for u in ok:
        assert K.url_problem(u) is None, u
    for u in ("", None, "ftp://github.com", "https://", "https://user:pw@github.com/x", "https://github.com/a b",
              "https://TODO", "https://sub.example.org/x", "https://[2001:db8::1]/x", "https://10.0.0.1/x", "https://docs.example/x"):
        assert K.url_problem(u) is not None, u


def test_frontmatter_parser_unit():
    assert K.parse_frontmatter("---\na: 1\nb: 'x y'\n---\n# t\n") == ({"a": "1", "b": "x y"}, 4, "")
    assert K.parse_frontmatter("# t\n")[0] is None
    assert K.parse_frontmatter("---\na: 1\n")[2] == "the frontmatter has no closing ---"
    assert "is given twice" in K.parse_frontmatter("---\na: 1\na: 2\n---\n")[2]
    assert "is not `key: value`" in K.parse_frontmatter("---\n- a\n---\n")[2]


# ---- provider-links (advisory) -----------------------------------------------------------------------------------------
@pytest.fixture
def stub_site():
    """A loopback HTTP server: /ok answers 200, /moved 301 to /ok, /nohead refuses HEAD (405) and answers GET, /gone 404."""
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def _answer(self, head: bool):
            path = self.path.split("?")[0]
            seen.append((self.command, path))
            if path == "/moved":
                self.send_response(301)
                self.send_header("Location", "/ok")
            elif path == "/nohead" and head:
                self.send_response(405)
            elif path in ("/ok", "/nohead"):
                self.send_response(200)
            else:
                self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_HEAD(self):
            self._answer(True)

        def do_GET(self):
            self._answer(False)

        def log_message(self, *a):
            pass

    seen: list = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, seen
    srv.shutdown()
    srv.server_close()


def _point_links_at(monkeypatch, srv, mapping):
    port = srv.server_address[1]
    monkeypatch.setattr(K, "LINK_REWRITE", lambda url: f"http://127.0.0.1:{port}{mapping.get(url, '/gone')}")


def test_provider_links_skip_offline_and_by_default():
    card = K.check_bobbin(os.path.join(FIX, "kitcode"), only=["provider-links"])
    c, = card.checks
    assert c.result == "skip" and not c.required and "--online" in c.detail


def test_provider_links_green_follows_redirects_and_falls_back_to_get(monkeypatch, stub_site):
    srv, seen = stub_site
    _point_links_at(monkeypatch, srv, {"https://github.com": "/ok", "https://docs.github.com/en/webhooks": "/moved",
                                       "https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries":
                                       "/nohead"})
    c = run(os.path.join(FIX, "kitcode"), "provider-links", online=True)["provider-links"]
    assert c.result == "pass" and c.detail == "3 provider page(s) answer 2xx/3xx", (c.detail, messages(c))
    assert ("HEAD", "/moved") in seen and ("HEAD", "/ok") in seen and ("GET", "/nohead") in seen


def test_provider_links_red_is_advisory(monkeypatch, stub_site, tmp_path):
    srv, _seen = stub_site
    _point_links_at(monkeypatch, srv, {"https://github.com": "/ok"})          # the two docs pages answer 404
    d = copy(tmp_path, "kitcode")
    card = K.check_bobbin(d, only=["provider", "provider-links"], online=True)
    by = {c.name: c for c in card.checks}
    c = by["provider-links"]
    assert c.result == "fail" and not c.required and card.ok, "a provider's site being down never blocks a merge"
    assert sorted(f.message.split(":")[0] for f in c.findings) == ["provider.signature_docs", "provider.webhook_docs"]
    assert all("answered HTTP 404" in f.message and f.file == "bobbin.toml" and f.line > 1 for f in c.findings)
    assert not K.check_bobbin(d, only=["provider-links"], online=True, strict=True).ok


def test_the_command_takes_online(monkeypatch, stub_site, capsys):
    srv, _seen = stub_site
    _point_links_at(monkeypatch, srv, {})
    assert cli(["bobbin", "check", os.path.join(FIX, "kittpl"), "--online", "--report", "json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    links = next(c for c in doc["checks"] if c["name"] == "provider-links")
    assert links["result"] == "fail" and not links["required"]


# ---- the vendor directory and sibling bobbins ------------------------------------------------------------------------
def test_core_only_red_for_code_in_the_vendor_directory(tmp_path):
    d = copy(tmp_path, "kitcode")
    vendor = os.path.dirname(d)
    write(vendor, "__init__.py", "")
    assert run(d, "core-only")["core-only"].result == "pass", "an empty vendor __init__.py is fine"
    write(vendor, "__init__.py", "SHARED = 1\n")
    write(vendor, "helpers.py", "def shared():\n    return 1\n")
    c = run(d, "core-only")["core-only"]
    assert c.result == "fail"
    assert any("vendor's __init__.py must be empty" in m for m in messages(c))
    assert any("code in the vendor directory" in m for m in messages(c))


def test_core_only_red_for_a_bare_import_of_a_sibling_bobbin(tmp_path):
    d = copy(tmp_path, "kitcode")
    copy(tmp_path, "kitproc")
    with open(os.path.join(d, "source.py"), "a") as f:
        f.write("\nimport kitproc\n")
    c = run(d, "core-only")["core-only"]
    assert c.result == "fail" and any("kitproc/ is another bobbin of the same vendor" in m for m in messages(c))


def test_core_only_green_for_a_stdlib_module_named_like_a_sibling(tmp_path):
    d = copy(tmp_path, "kitcode")
    os.makedirs(os.path.join(os.path.dirname(d), "calendar"))       # a sibling bobbin named after a stdlib module
    with open(os.path.join(d, "source.py"), "a") as f:
        f.write("\nimport calendar  # noqa: F401\n")
    assert run(d, "core-only")["core-only"].result == "pass"
