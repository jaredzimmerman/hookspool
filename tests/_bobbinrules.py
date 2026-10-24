"""The ARCHITECTURE 14.3.7 rules for the modules of a bobbin, as an AST walk: a bobbin calls core
(``ctx.http``, ``ctx.oauth``, ``ctx.load_state``, ``hookspool.providers``) and never re-implements it, so its
own modules import none of the listed libraries, call none of the listed builtins and ``os`` functions, touch
neither ``sys.path`` nor ``os.environ``, and never import a bobbin. Hooks modules are deployment code and are
not checked.

This is the check the shared conformance kit runs for every bobbin; it lives here until that kit does."""
from __future__ import annotations

import ast
import os

BANNED_IMPORTS = frozenset({
    "hmac", "urllib", "http", "requests", "httpx", "aiohttp", "socket", "ssl", "subprocess", "asyncio",
    "threading", "multiprocessing", "signal", "sqlite3", "fcntl", "tempfile", "shutil", "pickle", "logging",
    "importlib"})
BANNED_CALLS = frozenset({"open", "print", "exec", "eval", "compile", "__import__"})
BANNED_METHODS = frozenset({"write_text", "write_bytes", "open", "unlink"})


def _chain(node: ast.AST) -> list[str]:
    """``a.b.c`` as ["a", "b", "c"] (empty when the base is not a plain name)."""
    out: list[str] = []
    while isinstance(node, ast.Attribute):
        out.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        out.append(node.id)
        return out[::-1]
    return []


def violations_in(source: str, filename: str = "<bobbin>") -> list[str]:
    found: list[str] = []
    tree = ast.parse(source, filename)

    def bad(node: ast.AST, what: str) -> None:
        found.append(f"{filename}:{getattr(node, 'lineno', 0)}: {what}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in BANNED_IMPORTS:
                    bad(node, f"imports {a.name}")
                if a.name == "hookspool.bobbins" or a.name.startswith("hookspool.bobbins."):
                    bad(node, f"imports {a.name} (a bobbin never imports a bobbin)")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if node.level:
                bad(node, "relative import (a bobbin's files are siblings loaded by the runtime)")
            if mod.split(".")[0] in BANNED_IMPORTS:
                bad(node, f"imports from {mod}")
            if mod == "hookspool.bobbins" or mod.startswith("hookspool.bobbins."):
                bad(node, f"imports from {mod} (a bobbin never imports a bobbin)")
            if mod == "hookspool" and any(a.name == "bobbins" for a in node.names):
                bad(node, "imports hookspool.bobbins")
            if mod == "os" and any(a.name != "path" for a in node.names):
                bad(node, "imports from os something other than os.path")
            if mod == "time" and any(a.name == "sleep" for a in node.names):
                bad(node, "imports time.sleep")
            if mod == "sys" and any(a.name == "path" for a in node.names):
                bad(node, "imports sys.path")
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in BANNED_CALLS:
                bad(node, f"calls {f.id}()")
            if isinstance(f, ast.Attribute):
                if f.attr in BANNED_METHODS:
                    bad(node, f"calls .{f.attr}()")
                ch = _chain(f)
                if ch[:1] == ["os"] and ch[1:2] != ["path"]:
                    bad(node, f"calls {'.'.join(ch)}()")
                if ch == ["time", "sleep"]:
                    bad(node, "calls time.sleep()")
        elif isinstance(node, ast.Attribute):
            ch = _chain(node)
            if ch[:2] == ["sys", "path"]:
                bad(node, "uses sys.path")
            if ch[:2] == ["os", "environ"]:
                bad(node, "uses os.environ")
    return sorted(set(found))


def violations(bobbin_dir: str) -> list[str]:
    """Every violation in the ``.py`` files of one bobbin directory."""
    out: list[str] = []
    for name in sorted(os.listdir(bobbin_dir)):
        if name.endswith(".py"):
            with open(os.path.join(bobbin_dir, name), encoding="utf-8") as f:
                out.extend(violations_in(f.read(), name))
    return out
