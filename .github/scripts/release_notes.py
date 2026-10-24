"""Check a release tag against the package version and print that version's changelog section.

    python .github/scripts/release_notes.py --tag v0.2.0 [--out notes.md]

Exit 0 when the tag is ``v`` + ``hookspool.__version__``, the version is a final release (no ``.dev``, ``a``, ``b`` or
``rc`` part), and CHANGELOG.md has a dated ``## [<version>] - YYYY-MM-DD`` section with text in it. The section body
(without its heading) goes to ``--out``, or to stdout. Anything else exits 1 with the reason on stderr, so the release
workflow stops before it builds. Standard library only; it reads the files and imports nothing from the package.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"\s*$', re.M)
FINAL_RE = re.compile(r"\d+\.\d+\.\d+")


def package_version(root: str = ROOT) -> str:
    with open(os.path.join(root, "hookspool", "__init__.py"), encoding="utf-8") as f:
        m = VERSION_RE.search(f.read())
    if not m:
        raise ValueError("hookspool/__init__.py has no __version__ line")
    return m.group(1)


def changelog_section(text: str, version: str) -> str:
    """The body of ``## [<version>] - <date>``, up to the next ``## `` heading or the link definitions."""
    head = re.search(rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}\s*$", text, re.M)
    if not head:
        raise ValueError(f"CHANGELOG.md has no dated '## [{version}] - YYYY-MM-DD' heading")
    rest = text[head.end():]
    end = re.search(r"^## |^\[[^\]]+\]: ", rest, re.M)
    body = (rest[:end.start()] if end else rest).strip()
    if not body:
        raise ValueError(f"CHANGELOG.md section {version} is empty")
    return body + "\n"


def top_release(text: str) -> str | None:
    """The newest released version in the changelog (the first heading after [Unreleased])."""
    m = re.search(r"^## \[(\d+\.\d+\.\d+)\]", text, re.M)
    return m.group(1) if m else None


def check(tag: str, root: str = ROOT) -> str:
    version = package_version(root)
    if tag != f"v{version}":
        raise ValueError(f"tag {tag!r} does not match hookspool.__version__ {version!r} (expected 'v{version}')")
    if not FINAL_RE.fullmatch(version):
        raise ValueError(f"{version!r} is not a final release version (X.Y.Z)")
    with open(os.path.join(root, "CHANGELOG.md"), encoding="utf-8") as f:
        text = f.read()
    if top_release(text) != version:
        raise ValueError(f"the newest CHANGELOG.md release is {top_release(text)!r}, not {version!r}")
    return changelog_section(text, version)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tag", required=True, help="the pushed tag, e.g. v0.2.0")
    ap.add_argument("--out", help="write the notes here instead of stdout")
    ap.add_argument("--root", default=ROOT, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    try:
        notes = check(a.tag, a.root)
    except (OSError, ValueError) as e:
        print(f"release: {e}", file=sys.stderr)
        return 1
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(notes)
    else:
        sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
