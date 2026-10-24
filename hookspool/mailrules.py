"""Mail rules: recognise a message by sender and subject, and pull a few named values out of it.

A mail bobbin uses this to turn the mail of services that have no API and no push (orders, deliveries,
reservations, court alerts) into one typed item, without a bobbin per sender. It is plain data and plain
regular expressions: no network, no model, no state.

A rule is a table::

    name         the name the item carries as ``rule`` (required, unique)
    from         sender domains or addresses; a domain matches its subdomains; an entry with an @ is one exact address
    subject_any  case-insensitive substrings of the subject; one is enough
    subject_regex  a case-insensitive regular expression searched in the subject
    kind         free text for the consumer: order, delivery, receipt, reservation, cancellation, alert, ...
    extract      name -> regular expression with exactly ONE capture group; tried on the subject, then on the body
    skip         case-insensitive words or phrases; one of them in the subject or the body vetoes the rule
    skip_subject case-insensitive words or phrases; one of them in the subject (only) vetoes the rule

``skip`` and ``skip_subject`` match whole words (``sale`` does not veto "Wholesale" or "Salem"), and they do not look
inside double-quoted text in the subject (an item name or a saved search: 'Shipped: "Deal Kit"' is not vetoed by ``deal``).

``subject_any`` and ``subject_regex`` together match when either does. A rule needs at least a ``from``,
a ``subject_any`` or a ``subject_regex``. Rules are tried in order and the first that matches wins, so a message
matches at most one rule. A rule pack is a TOML file ``rule-packs/<name>.toml`` in a bobbin's directory::

    [pack]
    name = "..."  status = "verified" | "unverified"  description = "..."
    [[rules]]  (the fields above; its item's rule is "<pack>.<name>")

Nothing in an error message quotes a mail or a configured pattern; they name keys and rules only.
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field

PACK_DIR = "rule-packs"
PACK_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
PACK_STATUS = ("verified", "unverified")
RULE_KEYS = {"name", "from", "subject_any", "subject_regex", "kind", "extract", "skip", "skip_subject"}
BODY_LIMIT = 20000          # characters of body a pattern ever sees (bounds the cost of a pattern)
VALUE_LIMIT = 200           # characters kept of one extracted value
MAX_RULES = 500


@dataclass(frozen=True)
class Rule:
    name: str
    senders: tuple[str, ...] = ()
    subject_any: tuple[str, ...] = ()
    subject_regex: re.Pattern | None = None
    kind: str = ""
    extract: dict = field(default_factory=dict)       # name -> compiled pattern
    skip: tuple[str, ...] = ()
    skip_subject: tuple[str, ...] = ()
    builtin: bool = False                             # a rule of a built-in pack: never claims bulk (promotions/social) mail


@dataclass(frozen=True)
class Pack:
    name: str
    status: str
    description: str
    rules: tuple[Rule, ...]


def _strings(raw, where: str, key: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)) or not all(isinstance(v, str) for v in raw):
        raise ValueError(f"{where}: {key} must be a list of strings")
    return tuple(v.strip() for v in raw if v.strip())


def parse_rule(raw, where: str, prefix: str = "") -> Rule:
    """One rule table -> a ``Rule``. ``ValueError`` names the rule and the key, never a value."""
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: a rule must be a table")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{where}: a rule needs a name")
    where = f"{where} {name.strip()!r}"
    unknown = sorted(set(raw) - RULE_KEYS)
    if unknown:
        raise ValueError(f"{where}: unknown key {unknown[0]!r}")
    senders = tuple(s.lower().lstrip("@") if "@" not in s.strip("@") else s.lower()
                    for s in _strings(raw.get("from"), where, "from"))
    subject_any = tuple(s.lower() for s in _strings(raw.get("subject_any"), where, "subject_any"))
    skip = tuple(s.lower() for s in _strings(raw.get("skip"), where, "skip"))
    skip_subject = tuple(s.lower() for s in _strings(raw.get("skip_subject"), where, "skip_subject"))
    rx = raw.get("subject_regex")
    subject_regex = None
    if rx not in (None, ""):
        if not isinstance(rx, str):
            raise ValueError(f"{where}: subject_regex must be a string")
        try:
            subject_regex = re.compile(rx, re.I)
        except (re.error, OverflowError, RecursionError):      # a huge repeat count raises OverflowError, not re.error
            raise ValueError(f"{where}: subject_regex is not a valid regular expression") from None
    if not (senders or subject_any or subject_regex):
        raise ValueError(f"{where}: needs from, subject_any or subject_regex")
    kind = raw.get("kind", "")
    if not isinstance(kind, str):
        raise ValueError(f"{where}: kind must be a string")
    ex = raw.get("extract") or {}
    if not isinstance(ex, dict):
        raise ValueError(f"{where}: extract must be a table of name = regular expression")
    patterns = {}
    for key, pat in ex.items():
        if not isinstance(pat, str) or not pat:
            raise ValueError(f"{where}: extract.{key} must be a regular expression")
        try:
            c = re.compile(pat, re.I)
        except (re.error, OverflowError, RecursionError):
            raise ValueError(f"{where}: extract.{key} is not a valid regular expression") from None
        if c.groups != 1:
            raise ValueError(f"{where}: extract.{key} must have exactly one capture group")
        patterns[str(key)] = c
    return Rule(prefix + name.strip(), senders, subject_any, subject_regex, kind.strip(), patterns, skip, skip_subject, bool(prefix))


def parse_rules(raw, where: str = "rules") -> list[Rule]:
    """The ``rules`` setting -> rules, in order."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"{where}: must be a list of rule tables")
    if len(raw) > MAX_RULES:
        raise ValueError(f"{where}: more than {MAX_RULES} rules")
    return [parse_rule(r, where) for r in raw]


def pack_dir(bobbin_dir: str) -> str:
    return os.path.join(bobbin_dir, PACK_DIR)


def _read_pack(path: str, name: str) -> Pack:
    try:
        with open(path, "rb") as f:
            doc = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        raise ValueError(f"rule pack {name!r}: cannot be read") from None
    head = doc.get("pack")
    if not isinstance(head, dict) or head.get("name") != name:
        raise ValueError(f"rule pack {name!r}: the [pack] table must carry name = {name!r}")
    status = head.get("status")
    if status not in PACK_STATUS:
        raise ValueError(f"rule pack {name!r}: status must be one of {', '.join(PACK_STATUS)}")
    desc = head.get("description")
    if not isinstance(desc, str) or not desc.strip():
        raise ValueError(f"rule pack {name!r}: needs a description")
    rules = doc.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError(f"rule pack {name!r}: needs at least one [[rules]] table")
    parsed = tuple(parse_rule(r, f"rule pack {name!r}", prefix=name + ".") for r in rules)
    return Pack(name, status, desc.strip(), parsed)


def pack_names(directory: str) -> list[str]:
    """The packs a directory holds (``<directory>/*.toml``), sorted."""
    try:
        files = sorted(os.listdir(directory))
    except OSError:
        return []
    return [f[:-5] for f in files if f.endswith(".toml") and PACK_NAME.match(f[:-5])]


def load_pack(directory: str, name: str) -> Pack:
    if not isinstance(name, str) or not PACK_NAME.match(name):
        raise ValueError("rule_packs: a pack name is lower-case letters, digits and hyphens")
    known = pack_names(directory)
    if name not in known:
        raise ValueError(f"rule_packs: no built-in pack {name!r} (known: {', '.join(known) or 'none'})")
    return _read_pack(os.path.join(directory, name + ".toml"), name)


def summaries(directory: str) -> list[dict]:
    """``[{name, status, description, rules}]`` for ``bobbin show``; an unreadable pack is listed with its error."""
    out = []
    for n in pack_names(directory):
        try:
            p = _read_pack(os.path.join(directory, n + ".toml"), n)
            out.append({"name": n, "status": p.status, "description": p.description,
                        "rules": [r.name for r in p.rules]})
        except ValueError as e:
            out.append({"name": n, "status": "invalid", "description": str(e), "rules": []})
    return out


def compile_rules(custom, pack_list, directory: str) -> list[Rule]:
    """Custom rules first, then each listed pack in order. Names must be unique."""
    rules = parse_rules(custom)
    if pack_list in (None, ""):
        pack_list = []
    if not isinstance(pack_list, (list, tuple)):
        raise ValueError("rule_packs: must be a list of pack names")
    seen_packs = set()
    for n in pack_list:
        if not isinstance(n, str):                     # a table or a list here is unhashable: say so as a config error
            raise ValueError("rule_packs: each entry must be a pack name (a string)")
        if n in seen_packs:
            raise ValueError(f"rule_packs: {n!r} is listed twice")
        seen_packs.add(n)
        rules += load_pack(directory, n).rules
    seen = set()
    for r in rules:
        if r.name in seen:
            raise ValueError(f"rules: the name {r.name!r} is used twice")
        seen.add(r.name)
    return rules


# ---- matching ---------------------------------------------------------------------------------------------------
def _sender_hit(address: str, entries: tuple[str, ...]) -> bool:
    domain = address.rsplit("@", 1)[-1]
    for e in entries:
        if "@" in e:
            if address == e:
                return True
        elif domain == e or domain.endswith("." + e):
            return True
    return False


# A straight quote straight after a digit is an inch mark (24" monitor), not the start of a quoted span.
_QUOTED = re.compile(r'(?<!\d)"[^"\n]*(?:"|$)|\u201c[^\u201d\n]*(?:\u201d|$)|\u00ab[^\u00bb\n]*(?:\u00bb|$)')


def _unquoted(subject: str) -> str:
    """The subject without its double-quoted spans (item names, saved-search queries), which never veto a rule.
    A quote the sender cut off (no closing mark) runs to the end of the subject."""
    return _QUOTED.sub(" ", subject)


def _word_hit(phrase: str, text: str) -> bool:
    """Is ``phrase`` in ``text`` as whole words? An edge that is a letter or digit must not touch another one, so
    ``sale`` is not found in "wholesale" and ``% off`` is still found in "50% off". Both are lower case."""
    at = text.find(phrase)
    while at != -1:
        end = at + len(phrase)
        left_ok = not phrase[0].isalnum() or at == 0 or not text[at - 1].isalnum()
        right_ok = not phrase[-1].isalnum() or end == len(text) or not text[end].isalnum()
        if left_ok and right_ok:
            return True
        at = text.find(phrase, at + 1)
    return False


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()[:VALUE_LIMIT]


def matches(rule: Rule, address: str, subject: str, body: str) -> bool:
    """Does this rule claim the message? ``address`` is the sender's bare lower-case address."""
    if rule.senders and not _sender_hit(address, rule.senders):
        return False
    subj = subject.lower()
    if rule.subject_any or rule.subject_regex:
        by_text = any(s in subj for s in rule.subject_any)
        by_regex = rule.subject_regex is not None and rule.subject_regex.search(subject) is not None
        if not (by_text or by_regex):
            return False
    if rule.skip_subject or rule.skip:
        veto_subj = _unquoted(subj)
        if any(_word_hit(s, veto_subj) for s in rule.skip_subject):
            return False
        if rule.skip:
            hay = veto_subj + "\n" + body[:BODY_LIMIT].lower()
            if any(_word_hit(s, hay) for s in rule.skip):
                return False
    return True


def extract(rule: Rule, subject: str, body: str) -> dict:
    """Each ``extract`` name that matched, as its first capture (subject first, then the body); the rest are left out."""
    text = body[:BODY_LIMIT]
    out = {}
    for key, pat in rule.extract.items():
        for source in (subject, text):
            m = pat.search(source)
            if m and m.group(1) is not None and _clean(m.group(1)):
                out[key] = _clean(m.group(1))
                break
    return out


def first_match(rules, address: str, subject: str, body: str, bulk: bool = False) -> tuple[Rule, dict] | None:
    """The first rule that claims the message, with its extracted values; None when none does.

    ``bulk`` marks mail Gmail filed as promotions, social or forums: the rules of the built-in packs leave it alone
    (a custom rule may still claim it)."""
    for r in rules:
        if bulk and r.builtin:
            continue
        if matches(r, address, subject, body):
            return r, extract(r, subject, body)
    return None
