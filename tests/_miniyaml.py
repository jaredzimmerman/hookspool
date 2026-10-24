"""A small YAML reader for the workflow tests (PyYAML is not a dependency).

It reads the subset GitHub workflow files here use: block mappings and sequences by indentation,
``- key: value`` items, plain / single- / double-quoted scalars, ``[a, b]`` flow lists, ``|`` block
scalars and comments. ``on`` stays the string ``"on"`` (PyYAML would make it ``True``). Anything
outside the subset raises ``ValueError``, so a test never passes on a misread file.
"""
from __future__ import annotations

import re
from typing import Any


def _strip_comment(s: str) -> str:
    q = None
    for i, ch in enumerate(s):
        if q:
            if ch == q:
                q = None
        elif ch in "'\"":
            q = ch
        elif ch == "#" and (i == 0 or s[i - 1] in " \t"):
            return s[:i].rstrip()
    return s.rstrip()


def _scalar(s: str) -> Any:
    s = s.strip()
    if s == "":
        return None
    if s[0] == s[-1] == '"' and len(s) >= 2:
        return s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if s[0] == s[-1] == "'" and len(s) >= 2:
        return s[1:-1].replace("''", "'")
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [_scalar(x) for x in re.split(r",\s*", inner)] if inner else []
    if s.startswith(("{", "&", "*", "!")):
        raise ValueError(f"unsupported YAML: {s[:40]!r}")
    if s in ("true", "false"):
        return s == "true"
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


class _Lines:
    def __init__(self, text: str):
        self.items: list[tuple[int, str, int]] = []      # (indent, content, raw index)
        self.raw = text.split("\n")
        for n, ln in enumerate(self.raw):
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            if "\t" in ln[: len(ln) - len(ln.lstrip())]:
                raise ValueError("tab indentation")
            self.items.append((len(ln) - len(ln.lstrip()), ln.strip(), n))


def load(text: str) -> Any:
    lines = _Lines(text)
    value, i = _block(lines, 0, 0)
    if i != len(lines.items):
        raise ValueError(f"unparsed YAML from line {lines.items[i][2] + 1}")
    return value


def _block_scalar(lines: _Lines, i: int, parent_indent: int) -> tuple[str, int]:
    """The ``|`` text after item ``i - 1``: every following raw line indented deeper than the parent."""
    start = lines.items[i - 1][2] + 1
    out, n = [], start
    while n < len(lines.raw) and (not lines.raw[n].strip() or
                                  len(lines.raw[n]) - len(lines.raw[n].lstrip()) > parent_indent):
        out.append(lines.raw[n])
        n += 1
    while i < len(lines.items) and lines.items[i][2] < n:
        i += 1
    ind = min((len(x) - len(x.lstrip()) for x in out if x.strip()), default=0)
    return "\n".join(x[ind:] for x in out).rstrip("\n") + "\n", i


def _value_after(lines: _Lines, i: int, rest: str, indent: int) -> tuple[Any, int]:
    rest = _strip_comment(rest)
    if rest in ("|", "|-", ">", ">-"):
        return _block_scalar(lines, i, indent)
    if rest:
        return _scalar(rest), i
    if i < len(lines.items) and (lines.items[i][0] > indent or
                                 (lines.items[i][0] == indent and lines.items[i][1].startswith("- "))):
        return _block(lines, i, lines.items[i][0])
    return None, i


def _block(lines: _Lines, i: int, indent: int) -> tuple[Any, int]:
    if i >= len(lines.items):
        return None, i
    if lines.items[i][1].startswith("- ") or lines.items[i][1] == "-":
        return _seq(lines, i, indent)
    return _map(lines, i, indent)


def _key(content: str) -> tuple[str, str]:
    m = re.match(r"""^("[^"]*"|'[^']*'|[^:'"]+?):(?:\s+(.*)|$)""", content)
    if not m:
        raise ValueError(f"not a mapping entry: {content[:60]!r}")
    k = m.group(1)
    return (k[1:-1] if k[0] in "'\"" else k.strip()), (m.group(2) or "")


def _map(lines: _Lines, i: int, indent: int) -> tuple[dict, int]:
    out: dict = {}
    while i < len(lines.items):
        ind, content, _n = lines.items[i]
        if ind < indent:
            break
        if ind > indent:
            raise ValueError(f"bad indentation at line {_n + 1}")
        if content.startswith("- "):
            break
        k, rest = _key(content)
        if k in out:
            raise ValueError(f"duplicate key {k!r}")
        out[k], i = _value_after(lines, i + 1, rest, indent)
    return out, i


def _seq(lines: _Lines, i: int, indent: int) -> tuple[list, int]:
    out: list = []
    while i < len(lines.items):
        ind, content, n = lines.items[i]
        if ind != indent or not (content.startswith("- ") or content == "-"):
            break
        rest = content[1:].strip()
        if not rest:
            v, i = _block(lines, i + 1, lines.items[i + 1][0]) if i + 1 < len(lines.items) else (None, i + 1)
            out.append(v)
            continue
        if re.match(r"""^("[^"]*"|'[^']*'|[^:'"\[{]+?):(\s|$)""", rest):
            # a mapping item: its first key is on the dash line, the rest at the dash's indent + 2
            sub_indent = indent + 2
            lines.items[i] = (sub_indent, rest, n)
            v, i = _map(lines, i, sub_indent)
            out.append(v)
        else:
            out.append(_scalar(_strip_comment(rest)))
            i += 1
    return out, i
