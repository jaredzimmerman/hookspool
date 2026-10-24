"""Atom and RSS feed parsing for pollers that read a service's feed (ARCHITECTURE 14.4).

``parse_feed(text)`` turns an Atom 1.0, RSS 2.0 or RSS 1.0 (RDF) document into plain dicts, so a bobbin never
parses XML itself and every bobbin gets the same hardening. The input is untrusted, so the rules are strict:

- A document type declaration of any kind is refused (``FeedError``), and with it every entity declaration:
  there is no entity expansion, no external entity, no external DTD. A feed has no use for them, and refusing
  the declaration is what stops a billion-laughs or file-reading document before it is parsed at all.
- The size, the element depth, the element count and the entry count are capped.
- Only the standard library is used: ``xml.parsers.expat`` makes the refusing pass, ``xml.etree`` builds the tree.
- Error text never carries the document's content (a ``FeedError`` names the rule and a line number), so it is
  safe in a log.

Result::

    {"format": "atom" | "rss", "id": ..., "title": ..., "link": ..., "updated": ..., "subtitle": ...,
     "entries": [{"id", "title", "link", "updated", "published", "summary", "content", "author",
                  "categories": [{"term", "label", "scheme"}, ...], "fields": {name: text, ...}}, ...]}

Every value is a string (``""`` when absent), ``categories`` a list and ``fields`` a dict. ``fields`` holds the
text of the direct children of an Atom ``<content>`` element and of entry children in a namespace the format does not
define (``accession-number`` in an EDGAR feed, a Dublin Core ``date``), keyed by local name; the first one wins.
Dates stay as the feed wrote them; ``parse_time`` turns an RFC 3339 or RFC 822 date into unix seconds.
"""
from __future__ import annotations

import datetime as _dt
import email.utils
import re
import xml.etree.ElementTree as ET
import xml.parsers.expat as _expat
from typing import Any

__all__ = ["FeedError", "parse_feed", "parse_time", "MAX_FEED_BYTES", "MAX_ENTRIES"]

MAX_FEED_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 1000
MAX_DEPTH = 32
MAX_ELEMENTS = 200_000
MAX_TITLE_CHARS = 1000
MAX_TEXT_CHARS = 20_000
MAX_FIELD_CHARS = 2000
MAX_FIELDS = 64
MAX_CATEGORIES = 32

_ATOM_NS = "http://www.w3.org/2005/Atom"
_RSS1_NS = "http://purl.org/rss/1.0/"
_SPACE_RE = re.compile(r"\s+")


class FeedError(ValueError):
    """The document is not a feed this module accepts. The message names the rule, never the content."""


# ---- the refusing pass ------------------------------------------------------------------------------
def _check(data: bytes | str) -> None:
    """Parse once with handlers that refuse a DTD and bound the shape; builds nothing."""
    parser = _expat.ParserCreate()
    state = {"depth": 0, "elements": 0}

    def line() -> int:
        return int(parser.CurrentLineNumber)

    def doctype(*_a: Any) -> None:
        raise FeedError(f"a document type or entity declaration is refused (line {line()})")

    def start(_name: str, _attrs: Any) -> None:
        state["depth"] += 1
        state["elements"] += 1
        if state["depth"] > MAX_DEPTH:
            raise FeedError(f"elements nested more than {MAX_DEPTH} deep (line {line()})")
        if state["elements"] > MAX_ELEMENTS:
            raise FeedError(f"more than {MAX_ELEMENTS} elements")

    def end(_name: str) -> None:
        state["depth"] -= 1

    def external(*_a: Any) -> int:
        raise FeedError(f"an external entity is refused (line {line()})")

    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = doctype
    parser.NotationDeclHandler = doctype
    parser.ExternalEntityRefHandler = external
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        parser.Parse(data, True)
    except FeedError:
        raise
    except _expat.ExpatError as e:
        raise FeedError(f"not well-formed XML ({_expat.errors.messages.get(e.code, 'parse error')},"
                        f" line {e.lineno})") from None


# ---- tree helpers -----------------------------------------------------------------------------------
def _split(tag: Any) -> tuple[str, str]:
    """(namespace, local name) of an ElementTree tag; comments and processing instructions have a callable tag."""
    if not isinstance(tag, str):
        return "", ""
    if tag.startswith("{"):
        ns, _, local = tag[1:].partition("}")
        return ns, local
    return "", tag


def _kids(el: ET.Element, local: str, *, ns: str | None = None) -> list[ET.Element]:
    out = []
    for c in el:
        n, name = _split(c.tag)
        if name == local and (ns is None or n == ns):
            out.append(c)
    return out


def _first(el: ET.Element, *names: str) -> ET.Element | None:
    for name in names:
        for c in el:
            if _split(c.tag)[1] == name:
                return c
    return None


def _text(el: ET.Element | None, cap: int, *, collapse: bool = False) -> str:
    if el is None:
        return ""
    s = "".join(el.itertext())
    s = _SPACE_RE.sub(" ", s).strip() if collapse else s.strip()
    return s[:cap]


def _attr(el: ET.Element, name: str) -> str:
    return str(el.attrib.get(name, "")).strip()


def _link(entry: ET.Element, rss: bool) -> str:
    """Atom: the rel=alternate (or rel-less) link's href, else the first href. RSS: the link element's text."""
    if rss:
        return _text(_first(entry, "link"), 2048)
    first = ""
    for c in entry:
        if _split(c.tag)[1] != "link":
            continue
        href = _attr(c, "href")
        if not href:
            continue
        rel = _attr(c, "rel") or "alternate"
        if rel == "alternate":
            return href[:2048]
        first = first or href[:2048]
    return first


def _author(entry: ET.Element, rss: bool) -> str:
    if rss:
        return _text(_first(entry, "author", "creator"), MAX_TITLE_CHARS, collapse=True)
    a = _first(entry, "author")
    if a is None:
        return ""
    name = _first(a, "name")
    return _text(name if name is not None else a, MAX_TITLE_CHARS, collapse=True)


def _categories(entry: ET.Element, rss: bool) -> list[dict[str, str]]:
    out = []
    for c in entry:
        if _split(c.tag)[1] not in ("category", "subject"):
            continue
        if rss:
            term = _text(c, 200, collapse=True)
            item = {"term": term, "label": "", "scheme": _attr(c, "domain")}
        else:
            item = {"term": _attr(c, "term")[:200], "label": _attr(c, "label")[:200],
                    "scheme": _attr(c, "scheme")[:200]}
        if item["term"] or item["label"]:
            out.append(item)
        if len(out) >= MAX_CATEGORIES:
            break
    return out


def _fields(entry: ET.Element, std_ns: set[str]) -> dict[str, str]:
    """Text of the children of Atom ``<content>`` and of foreign-namespace entry children, keyed by local name."""
    out: dict[str, str] = {}

    def put(el: ET.Element) -> None:
        name = _split(el.tag)[1]
        if name and name not in out and len(out) < MAX_FIELDS:
            out[name] = _text(el, MAX_FIELD_CHARS)

    for c in entry:
        ns, name = _split(c.tag)
        if name == "content" and ns in std_ns:
            for sub in c:
                put(sub)
        elif ns and ns not in std_ns:
            put(c)
    return out


def _entry(el: ET.Element, rss: bool, std_ns: set[str]) -> dict[str, Any]:
    if rss:
        ident = _text(_first(el, "guid"), 2048)
        updated = _text(_first(el, "pubDate", "date"), 100)
        published = _text(_first(el, "pubDate", "date"), 100)
        summary = _text(_first(el, "description"), MAX_TEXT_CHARS)
        content = _text(_first(el, "encoded"), MAX_TEXT_CHARS)
    else:
        ident = _text(_first(el, "id"), 2048)
        updated = _text(_first(el, "updated"), 100)
        published = _text(_first(el, "published"), 100)
        summary = _text(_first(el, "summary"), MAX_TEXT_CHARS)
        c = _first(el, "content")
        content = _text(c, MAX_TEXT_CHARS) if c is not None and len(c) == 0 else ""
    return {"id": ident, "title": _text(_first(el, "title"), MAX_TITLE_CHARS, collapse=True),
            "link": _link(el, rss), "updated": updated, "published": published, "summary": summary,
            "content": content, "author": _author(el, rss), "categories": _categories(el, rss),
            "fields": _fields(el, std_ns)}


# ---- public ---------------------------------------------------------------------------------------
def parse_feed(data: str | bytes, *, max_bytes: int = MAX_FEED_BYTES, max_entries: int = MAX_ENTRIES) -> dict[str, Any]:
    """Parse an Atom or RSS document. Raises ``FeedError`` for anything else, for a refused construct (see the module
    docstring) and for a document over ``max_bytes`` or with more than ``max_entries`` entries."""
    if not isinstance(data, (str, bytes, bytearray)):
        raise FeedError("a feed is text or bytes")
    if isinstance(data, bytearray):
        data = bytes(data)
    size = len(data.encode("utf-8", "replace")) if isinstance(data, str) else len(data)
    if size > max_bytes:
        raise FeedError(f"the feed is over {max_bytes} bytes")
    if not data.strip():
        raise FeedError("the feed is empty")
    _check(data)
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise FeedError("not well-formed XML") from None
    ns, name = _split(root.tag)
    if name == "feed" and ns == _ATOM_NS:
        fmt, rss, std = "atom", False, {_ATOM_NS}
        head, items = root, _kids(root, "entry", ns=_ATOM_NS)
    elif name == "rss":
        channel = _first(root, "channel")
        if channel is None:
            raise FeedError("an RSS document without a channel")
        fmt, rss, std = "rss", True, {""}
        head, items = channel, _kids(channel, "item")
    elif name == "RDF":
        channel = _first(root, "channel")
        fmt, rss, std = "rss", True, {_RSS1_NS, ""}
        head, items = channel if channel is not None else root, _kids(root, "item")
    else:
        raise FeedError("the document is not an Atom or RSS feed")
    if len(items) > max_entries:
        raise FeedError(f"more than {max_entries} entries")
    out: dict[str, Any] = {
        "format": fmt,
        "id": _text(_first(head, "id"), 2048),
        "title": _text(_first(head, "title"), MAX_TITLE_CHARS, collapse=True),
        "subtitle": _text(_first(head, "subtitle", "description"), MAX_TITLE_CHARS, collapse=True),
        "link": _link(head, rss),
        "updated": _text(_first(head, "updated", "lastBuildDate", "pubDate"), 100),
        "entries": [_entry(i, rss, std) for i in items],
    }
    return out


def parse_time(value: str) -> float | None:
    """Unix seconds for an RFC 3339 / ISO 8601 date (Atom) or an RFC 822 date (RSS), or None when it is neither.
    A date with no zone is read as UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    dt = None
    try:
        dt = _dt.datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(s)
        except (TypeError, ValueError, IndexError):
            dt = None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.timestamp()
