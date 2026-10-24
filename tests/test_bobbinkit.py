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
FIX = os.path.join(ROOT, "tests", "fixtures", "bobbinkit")
FIXTURES = sorted(d for d in os.listdir(FIX) if os.path.isfile(os.path.join(FIX, d, "bobbin.toml")))


# ---- helpers -------------------------------------------------------------------------------------------
def copy(tmp_path, name: str, as_name: str | None = None) -> str:
    d = os.path.join(str(tmp_path), "bobbins", as_name or name)
    shutil.copytree(os.path.join(FIX, name), d)
    if as_name:
        for rel in ("bobbin.toml", "source.py", "SETUP.md", "AGENTS.md"):
            p = os.path.join(d, rel)
            if os.path.exists(p):
                text = open(p).read().replace(f'"{name}"', f'"{as_name}"').replace(f"bobbins.{name}]", f"bobbins.{as_name}]")
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
    for n in ("manifest", "files", "todos", "core-only", "docs", "captures", "source-kit"):
        assert by[n].result == "pass", (n, by[n].detail)
    assert by["processor"].result == ("pass" if name == "kitproc" else "skip")
    assert by["scope"].result == "skip" and not by["scope"].required
    assert card.summary().startswith(f"bobbin check {name}: PASS")


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
    assert f.file == "bobbin.toml" and f.line == 12 and "secrets.secret.bogus: unknown key" in f.message


def test_manifest_red_name_is_not_the_directory(tmp_path):
    d = copy(tmp_path, "kitcode")
    edit(d, "bobbin.toml", 'name = "kitcode"', 'name = "othername"')
    c = run(d, "manifest")["manifest"]
    assert c.result == "fail" and any("does not match its directory" in m for m in messages(c))


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
    assert "x-kitcode-signature, the signature header of bobbin 'kitcode'" in f.message
    assert "declare it in this bobbin's auth_headers" in f.message, "the fix for a shared generic header name"


@pytest.mark.parametrize("module", ["hookspool.bobbins.other_bobbin.source", "hookspool.bobbins",
                                    "_hookspool_bobbins.other_bobbin.source", "_hookspool_bobbinkit.other_bobbin.source"])
def test_manifest_red_source_module_that_is_another_bobbins_code(tmp_path, module):
    d = copy(tmp_path, "kitcode")
    edit(d, "bobbin.toml", 'module = "source"', f'module = "{module}"')
    c = run(d, "manifest")["manifest"]
    assert c.result == "fail"
    f, = c.findings
    assert f.file == "bobbin.toml" and f.line == 6, f.where()          # the ``module =`` line
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
    assert c.result == "fail" and any("calls api.github.com, the API host of bobbin 'kitcode'" in m
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
    assert [(f.file, f.line) for f in c.findings] == [("README.md", 3)]


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
    assert f.file == "AGENTS.md" and f.line == 22 and "not marked `outward`" in f.message
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
     "does not expand: bobbins.kitcode.settings.nope: bobbin 'kitcode' has no such setting"),
    ("AGENTS.md", "   ```toml\n", "   ```\n", "no ```toml block"),
    ("SETUP.md", "[bobbins.kitcode]\n", "[bobbins.kitcode]\nbobbin = \"other\"\n", "has no instance of bobbin"),
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
    d = os.path.join(str(tmp_path), "bobbins", name)
    os.makedirs(os.path.join(d, "captures"))
    src = open(os.path.join(ROOT, "tests", "kitplugins", f"{plugin}.py")).read()
    if constant_callback:
        src = src.replace('str(req.config.get("callback_url") or "")', repr(kits.CALLBACK_URL))
    write(d, "source.py", src)
    for f in os.listdir(os.path.join(ROOT, "tests", "captures", name)):
        shutil.copy(os.path.join(ROOT, "tests", "captures", name, f), os.path.join(d, "captures", f))
    write(d, "bobbin.toml", f'name = "{name}"\nversion = "1"\nsummary = "kit"\n[source]\nmodule = "source"\n'
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
    d = os.path.join(root, "bobbins", "kitcode")
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
    d = os.path.join(root, "hookspool", "bobbins", "kitcode")
    shutil.copytree(os.path.join(FIX, "kitcode"), d)
    write(root, "tests/bobbins/test_kitcode.py", "def test_ok():\n    pass\n")
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
    assert doc["bobbin"] == "kitcode" and doc["ok"] is False and doc["strict"] is False and doc["scope"] is None
    assert isinstance(doc["dir"], str) and doc["summary"].startswith("bobbin check kitcode: FAIL, 1 of 9")
    assert [c["name"] for c in doc["checks"]] == K.CHECK_NAMES
    for c in doc["checks"]:
        assert set(c) == {"name", "required", "result", "detail", "fix", "findings"}
        assert c["result"] in ("pass", "fail", "skip") and isinstance(c["required"], bool)
        assert isinstance(c["detail"], str) and isinstance(c["fix"], str)
        assert bool(c["fix"]) == (c["result"] == "fail")
        for f in c["findings"]:
            assert set(f) == {"file", "line", "message"} and isinstance(f["line"], int)
    todos = next(c for c in doc["checks"] if c["name"] == "todos")
    assert todos["findings"] == [{"file": "README.md", "line": 3, "message": "TODO placeholder"}]
    assert [c["required"] for c in doc["checks"]] == [True] * 9 + [False]


def test_markdown_and_text_reports(capsys):
    d = os.path.join(FIX, "kitcode")
    assert cli(["bobbin", "check", d, "--report", "md"]) == 0
    md = capsys.readouterr().out
    assert md.startswith("### Bobbin `kitcode`: PASS") and "| Check | Result | Detail |" in md
    assert re.search(r"^\| source-kit \| pass \| 16 cases", md, re.M) and "| scope (advisory) | skip |" in md
    assert cli(["bobbin", "check", d]) == 0
    text = capsys.readouterr().out
    assert text.splitlines()[0] == "bobbin check kitcode: PASS, 7 of 9 required checks pass, 2 not applicable"


def test_markdown_lists_findings_and_how_to_fix(tmp_path):
    d = copy(tmp_path, "kitcode")
    edit(d, "README.md", "Kit fixture:", "TODO Kit fixture:")
    md = K.render_md(K.check_bobbin(d, only=["todos"]).as_dict())
    assert "| todos | **FAIL** | fill in the TODOs (1 left). How to fix: fill in the TODOs |" in md
    assert "- todos: `README.md:3` TODO placeholder" in md


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
@pytest.mark.parametrize("flags", [[], ["--code"], ["--processor"], ["--set", "scheme=url-token", "--set", "header="],
                                   ["--set", "scheme=hmac-sha256-base64", "--set", "prefix="],
                                   ["--set", "scheme=shared-secret-header"], ["--set", "scheme=hmac-sha1-base64-url"]],
                         ids=["webhook", "code", "processor", "url-token", "base64", "shared-secret", "sha1-url"])
def test_bobbin_new_passes_every_check_but_the_todos(tmp_path, capsys, flags):
    assert cli(["bobbin", "new", "acme-hooks", "--dir", str(tmp_path), *flags]) == 0
    capsys.readouterr()
    d = os.path.join(str(tmp_path), "acme-hooks")
    card = K.check_bobbin(d)
    failed = [c.name for c in card.checks if c.result == "fail"]
    assert failed == ["todos"], K.render_text(card.as_dict())
    assert card.summary() == "bobbin check acme-hooks: FAIL, 1 of 9 required checks fail (todos)"
    assert next(c for c in card.checks if c.name == "todos").detail.startswith("fill in the TODOs")
    for base, _dirs, files in os.walk(d):                     # filling them in is all that is left
        for f in files:
            p = os.path.join(base, f)
            assert f.endswith((".toml", ".md", ".py", ".http")), f"the kit wrote {f} into the bobbin"
            text = open(p).read()
            open(p, "w").write(re.sub(r"\bTODO\b:? ?", "", text))
    assert K.check_bobbin(d).ok


def test_the_webhook_bobbin_passes_the_conformance_kit():
    card = K.check_bobbin(os.path.join(ROOT, "hookspool", "bobbins", "webhook"))
    assert card.ok, K.render_text(card.as_dict())
    assert [c.result for c in card.checks if c.name in ("captures", "source-kit", "docs")] == ["pass"] * 3


def test_bobbin_new_with_frozen_settings_passes_every_check_but_the_todos(tmp_path, capsys):
    assert cli(["bobbin", "new", "acme-hooks", "--dir", str(tmp_path), "--set", 'key=["id", "type"]',
                "--set", "dedup=header:X-Delivery-Id", "--set", 'ignore=["type=ping"]']) == 0
    capsys.readouterr()
    d = os.path.join(str(tmp_path), "acme-hooks")
    card = K.check_bobbin(d)
    assert [c.name for c in card.checks if c.result == "fail"] == ["todos"], K.render_text(card.as_dict())
    for base, _dirs, files in os.walk(d):
        for f in files:
            p = os.path.join(base, f)
            text = open(p).read()
            open(p, "w").write(re.sub(r"\bTODO\b:? ?", "", text))
    assert K.check_bobbin(d).ok
    with open(os.path.join(d, "bobbin.toml"), "rb") as f:
        tpl = tomllib.load(f)["source"]["template"]
    assert tpl["key"] == ["id", "type"] and tpl["dedup"] == "header:X-Delivery-Id" and tpl["ignore"] == ["type=ping"]


def test_the_default_test_secret_is_the_scaffolds():
    assert K.DEFAULT_TEST_SECRET == BL.SCAFFOLD_SECRET and "test" in K.DEFAULT_TEST_SECRET
