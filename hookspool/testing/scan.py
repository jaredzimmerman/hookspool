"""Patterns for secrets and personal data in committed files (stdlib only).

One set of patterns serves the repository scanner (``.github/scripts/plugin_ci.py scan``) and the
bobbin conformance kit's capture check (``hookspool.testing.bobbinkit``), so a capture that passes
``hookspool bobbin check`` also passes the scan in CI.

``secret_findings(text)`` and ``personal_findings(text)`` return ``(line, kind, excerpt)`` with
the excerpt shortened, so a finding never echoes a whole secret.
"""
from __future__ import annotations

import re

SECRET_PATTERNS = {
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "GitHub token": re.compile("|".join((r"\bgh[pousr]_[A-Za-z0-9]{30,}\b", r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"))),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "AWS access key id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "OpenAI/Anthropic-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{24,}"),
    "Telegram bot token": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "credentials in a URL": re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s:@]{3,}@[^/\s]+"),
    "assigned secret": re.compile(
        r"""(?i)\b(?:secret|token|password|passwd|api[_-]?key)\b["']?\s*[:=]\s*["']([A-Za-z0-9+/_=-]{24,})["']"""),
}
# an "assigned secret" whose value says it is not a real one
PLACEHOLDER_VALUE = re.compile(r"(?i)test|example|placeholder|dummy|sentinel|fake")
# A capture is a request signed with test keys; one from a JWT-signing sender carries a JWT by design.
JWT_OK_UNDER = ("tests/captures/",)
_JWT_OK_RE = re.compile(r"(?:^|/)bobbins/[^/]+/captures/")

IPV4_RE = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")
DOC_NETS = ((192, 0, 2), (198, 51, 100), (203, 0, 113))

# Personal data in captures: an address at a real-looking domain, a phone number.
EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})(?![A-Za-z0-9-])")
EMAIL_OK = re.compile(r"(?i)(?:^|\.)(?:example\.(?:com|org|net)|[a-z0-9-]+\.(?:test|example|invalid|localhost))$"
                      r"|^(?:example|test|invalid|localhost)$")
PHONE_RE = re.compile(
    r"(?<![\w.+-])(?:\+\d{1,3}[\s.-]?)?\(?\d{2,4}\)?[\s.-]\d{3,4}[\s.-]\d{3,4}(?![\w.-])"   # 555-123-4567, +1 (555) 123 4567
    r"|(?<![\w.+-])\+\d{10,14}(?![\w.-])")                                                   # +15551234567
FICTIONAL_PHONE = re.compile(r"555[\s.)-]*01\d\d(?!\d)")                                     # reserved for fiction


def jwt_allowed(rel: str) -> bool:
    return rel.startswith(JWT_OK_UNDER) or bool(_JWT_OK_RE.search(rel))


def ip_allowed(a: int, b: int, c: int, d: int) -> bool:
    if max(a, b, c, d) > 255:
        return True                     # not an address (a version string, say)
    if a == 127 or (a, b, c, d) == (0, 0, 0, 0) or (a, b, c) in DOC_NETS:
        return True
    if a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168) or (a == 169 and b == 254):
        return True                     # RFC 1918 / link-local: the SSRF tests need them, and they identify no one
    return a == 255 or a == 0           # masks, wildcards


def redact(s: str) -> str:
    return s if len(s) <= 12 else s[:6] + "..." + f"({len(s)} chars)"


def secret_findings(text: str, rel: str = "") -> list[tuple[int, str, str]]:
    """``(line, kind, excerpt)`` for every secret-looking value in ``text``."""
    out = []
    for i, ln in enumerate(text.splitlines(), 1):
        out.extend((i, kind, ex) for kind, ex in secret_hits(ln, rel))
    return out


def secret_hits(line: str, rel: str = "") -> list[tuple[str, str]]:
    out = []
    for kind, rx in SECRET_PATTERNS.items():
        if kind == "JWT" and jwt_allowed(rel):
            continue
        m = rx.search(line)
        if m and not (kind == "assigned secret" and PLACEHOLDER_VALUE.search(m.group(1))):
            out.append((kind, redact(m.group(0))))
    return out


def personal_findings(text: str) -> list[tuple[int, str, str]]:
    """``(line, kind, excerpt)`` for real-looking personal data: an e-mail address outside the
    reserved example domains, a phone number (the fictional 555-01xx range passes), a public
    IPv4 address."""
    out = []
    for i, ln in enumerate(text.splitlines(), 1):
        for m in EMAIL_RE.finditer(ln):
            if not EMAIL_OK.search(m.group(1).rstrip(".")):
                out.append((i, "e-mail address", "<address at " + m.group(1)[-40:] + ">"))
        for m in PHONE_RE.finditer(ln):
            if sum(c.isdigit() for c in m.group(0)) >= 10 and not FICTIONAL_PHONE.search(m.group(0)):
                out.append((i, "phone number", "<" + str(sum(c.isdigit() for c in m.group(0))) + " digits>"))
        for m in IPV4_RE.finditer(ln):
            if not ip_allowed(*(int(x) for x in m.groups())):
                out.append((i, "IP address", m.group(0)))
    return out
