"""MkDocs hooks for the documentation site (mkdocs.yml ``hooks:``).

The pages in docs/ are written to be read on GitHub first, so the site adapts them at build time instead of the
pages carrying site-only syntax:

* the navigation follows the docs themselves: the folder order of the table in docs/README.md, and inside each
  folder its README.md (the section index) then the pages in the order that index links them; a page the index does
  not link comes last, so a new page is never left out
* a relative link that leaves docs/ (``../../CHANGELOG.md``, ``../../deploy/``, a bobbin's directory) points at the
  file on GitHub, where it lives, instead of a page the site does not have
* a banner embed (``<picture>`` with a dark ``<source>`` and a light ``<img>``) becomes two Markdown images marked
  ``#only-light`` and ``#only-dark``, so the banner follows the site's palette toggle and its paths are rewritten
  like any other image
"""
from __future__ import annotations

import os
import re

FENCE = re.compile(r"(^```.*?^```[^\n]*$)", re.M | re.S)
LINK = re.compile(r"(?<!!)(\[[^\]]*\]\()([^)\s]+)(\))")
PICTURE = re.compile(r'<picture><source media="\(prefers-color-scheme: dark\)" srcset="([^"]+)"><img alt="([^"]+)" '
                     r'src="([^"]+)"[^>]*></picture>')
INDEX_ROW = re.compile(r"^\|\s*\[[^\]]+\]\(([a-z0-9-]+)/README\.md\)", re.M)


def _blob_base(config) -> str:
    repo = (config.get("repo_url") or "").rstrip("/")
    branch = (config.get("extra") or {}).get("source_branch", "main")
    return f"{repo}/blob/{branch}/" if repo else ""


def _md_links(text: str) -> list[str]:
    return [t.partition("#")[0] for _, t, _ in LINK.findall(text) if not re.match(r"[a-z]+://|mailto:|#", t)]


def build_nav(docs_dir: str) -> list:
    """The nav: Home, then each folder of docs/README.md's table with its index first."""
    def read(rel: str) -> str:
        with open(os.path.join(docs_dir, rel), encoding="utf-8") as f:
            return f.read()

    nav: list = ["README.md"]
    for folder in INDEX_ROW.findall(read("README.md")):
        index = f"{folder}/README.md"
        if not os.path.isfile(os.path.join(docs_dir, index)):
            continue
        pages = [index]
        for target in _md_links(read(index)):
            rel = os.path.normpath(os.path.join(folder, target)).replace(os.sep, "/")
            if rel.startswith(f"{folder}/") and rel.endswith(".md") and rel.count("/") == 1 and rel not in pages \
                    and os.path.isfile(os.path.join(docs_dir, rel)):
                pages.append(rel)
        for name in sorted(os.listdir(os.path.join(docs_dir, folder))):
            rel = f"{folder}/{name}"
            if name.endswith(".md") and rel not in pages:
                pages.append(rel)
        title = re.search(r"^# (.+?)\s*$", read(index), re.M)
        nav.append({title.group(1) if title else folder: pages})
    return nav


def on_config(config, **kwargs):
    if not config.get("nav"):
        config["nav"] = build_nav(config["docs_dir"])
    return config


def _rewrite(text: str, page_dir: str, docs_dir: str, blob: str) -> str:
    def picture(m: re.Match) -> str:
        dark, alt, light = m.groups()
        return f"![{alt}]({light}#only-light)![{alt}]({dark}#only-dark)"

    def link(m: re.Match) -> str:
        target = m.group(2)
        if not blob or re.match(r"[a-z]+://|mailto:|#", target):
            return m.group(0)
        rel, _, frag = target.partition("#")
        dest = os.path.normpath(os.path.join(page_dir, rel))
        if os.path.commonpath([dest, docs_dir]) == docs_dir:
            return m.group(0)
        repo_root = os.path.dirname(docs_dir)
        path = os.path.relpath(dest, repo_root).replace(os.sep, "/")
        url = blob + path
        if os.path.isdir(dest):
            url = url.replace("/blob/", "/tree/", 1)
        return f"{m.group(1)}{url}{'#' + frag if frag else ''}{m.group(3)}"

    out = []
    for i, part in enumerate(FENCE.split(text)):
        if i % 2:                       # a fenced code block: an example, left as written
            out.append(part)
        else:
            out.append(LINK.sub(link, PICTURE.sub(picture, part)))
    return "".join(out)


def on_page_markdown(markdown, page, config, files, **kwargs):
    docs_dir = os.path.abspath(config["docs_dir"])
    page_dir = os.path.dirname(os.path.join(docs_dir, page.file.src_path))
    return _rewrite(markdown, page_dir, docs_dir, _blob_base(config))
