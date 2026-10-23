#!/usr/bin/env python3
"""Gmail push -> is there a new message that asks the owner for their availability? If so, hand the
agent the thread facts plus the owner's real free windows, so it can write a draft reply.

Run by the hookspool ``script:gmail_delta`` destination (``gmail_delta.py --batch``) with a batch of
Pub/Sub notifications on stdin (one mailbox per batch); also runnable by hand with a notification or
the bare ``{"emailAddress": ..., "historyId": N}`` on stdin. No LLM here. Returns payloads for the
agent (event_type "scheduling", plus "agent-mail" and "agent-schedule" for the agent's own mailbox) or None.

Batch mode (``--batch``): the notifications of one mailbox are one pass (it reads Gmail's history from
the stored ``historyId``, so extra pushes cost nothing). Results leave as ``{"enqueue": ...}`` lines;
state writes are staged and applied only after the journal in ``$HOOKSPOOL_STATE_DIR`` records them
(``hookspool_batch.py``), so a retry never loses a result and never advances the history id twice. Every
matching message of the pass is reported (a coalesced batch can hold several). Run it with ``max_inflight = 1``
on its destination: nothing locks the state file between the read and the staged write being applied.

Configuration is by environment (the destination's ``env_file``); nothing deployment-specific is in
this file:
  GMAIL_DELTA_OWNER      the owner's mailbox (draft replies to scheduling requests); required
  GMAIL_DELTA_AGENT      the assistant's own mailbox (optional); GMAIL_DELTA_AGENT_STATE its state file name
  GMAIL_DELTA_CRED_DIR   directory of <address>.json OAuth credentials (client_id, client_secret, refresh_token); required
  GMAIL_DELTA_DATA       state directory (default $HOOKSPOOL_STATE_DIR): history ids, log
  GMAIL_DELTA_TZ         time zone for windows (default UTC); GMAIL_DELTA_HOURS working window "9:00-17:30"
  GMAIL_DELTA_OWN_DOMAINS comma list of domains whose mail is "from us"
  GMAIL_DELTA_EMAIL_CHECK optional script whose SKIP_*/IMPORTANT_* constants are reused as filters (parsed, not run)
  GMAIL_DELTA_SCHED_MODULE optional module with classify()/load()/ref_tag() for the agent's scheduling hand-off
  GMAIL_DELTA_API, GMAIL_DELTA_FREEBUSY   API URLs (for tests)
  HOOKSPOOL_DELTA_ROUTES JSON event_type -> destination, e.g. {"scheduling": "hermes:mail"}

State: $GMAIL_DELTA_DATA/gmail_push_state[_agent].json  {historyId, handled: [message ids]}
Log:   gmail_push.log
"""
import ast
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import threading  # noqa: E402
import gcal_delta as _g  # noqa: E402
import hookspool_batch as hb  # noqa: E402
from gcal_delta import _write_json, Failed  # noqa: E402


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


ME = _env("GMAIL_DELTA_OWNER").strip().lower()
AGENT = _env("GMAIL_DELTA_AGENT").strip().lower()
CRED_DIR = _env("GMAIL_DELTA_CRED_DIR")
OWN_DOMAINS = tuple(d.strip().lower().lstrip("@") for d in _env("GMAIL_DELTA_OWN_DOMAINS").split(",") if d.strip())
sched = None                                       # optional agent scheduling hand-off module
if _env("GMAIL_DELTA_SCHED_MODULE"):
    sched = __import__(_env("GMAIL_DELTA_SCHED_MODULE"))
_TOK_LOCK = threading.Lock()


def token_for(account: str) -> str:
    """Access token for any account in the shared credential dir (same refresh logic as gcal_delta)."""
    with _TOK_LOCK:
        old = _g.CRED
        _g.CRED = os.path.join(CRED_DIR, f"{account}.json") if CRED_DIR else ""
        try:
            return _g.access_token()
        finally:
            _g.CRED = old


def access_token() -> str:
    return token_for(ME)

DATA = _env("GMAIL_DELTA_DATA") or _env("HOOKSPOOL_STATE_DIR")
STATE = os.path.join(DATA, "gmail_push_state.json")
STATES = {a: p for a, p in ((ME, STATE), (AGENT, os.path.join(DATA, _env("GMAIL_DELTA_AGENT_STATE", "gmail_push_state_agent.json")))) if a}
LOG = os.path.join(DATA, "gmail_push.log")
GMAIL = _env("GMAIL_DELTA_API", "https://gmail.googleapis.com/gmail/v1/users/me/")
FREEBUSY = _env("GMAIL_DELTA_FREEBUSY", "https://www.googleapis.com/calendar/v3/freeBusy")
EMAIL_CHECK = _env("GMAIL_DELTA_EMAIL_CHECK")
TZ = ZoneInfo(_env("GMAIL_DELTA_TZ", "UTC"))


def _hours(text: str) -> tuple[int, int, int, int]:
    try:
        a, b = text.split("-")
        (h1, m1), (h2, m2) = (tuple(int(x) for x in t.strip().split(":")) for t in (a, b))
        return h1, m1, h2, m2
    except ValueError:
        return 9, 0, 17, 30


HOURS = _hours(_env("GMAIL_DELTA_HOURS", "9:00-17:30"))   # working window for proposed times
MIN_WINDOW_MIN = 45
DAYS_AHEAD = 7
SKIP_LABELS = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS", "SPAM", "TRASH"}
SCHEDULING = re.compile(
    r"\b(availability|available|avail|what (times?|days?) (work|suit)|times? that work|"
    r"schedule (a |an |the |some )?(call|meeting|time|chat|interview|conversation)|"
    r"propose (a |some )?times?|time slots?|slots?|your calendar|book (a |some )?time|"
    r"set up (a |some )?time|find (a |some )?time|when (are|would) you)\b", re.I)


def log(line: str) -> None:
    if not DATA:
        return
    os.makedirs(DATA, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')} {line}\n")


def _lists():
    """SKIP_* / IMPORTANT_* constants from email_check.py, by parsing its source."""
    out = {}
    if not EMAIL_CHECK:
        return out
    try:
        tree = ast.parse(open(EMAIL_CHECK, encoding="utf-8").read())
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name.startswith(("SKIP_", "IMPORTANT_")):
                    try:
                        out[name] = ast.literal_eval(node.value)
                    except Exception:
                        pass
    except Exception as e:
        log(f"could not parse email_check.py lists: {e}")
    return out


def gget(token: str, path: str, params: dict | None = None):
    url = GMAIL + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def header(msg: dict, name: str) -> str:
    for h in (msg.get("payload") or {}).get("headers") or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def body_text(msg: dict) -> str:
    """First text/plain part, decoded; falls back to the snippet."""
    def walk(p):
        if not p:
            return ""
        if p.get("mimeType") == "text/plain" and (p.get("body") or {}).get("data"):
            return base64.urlsafe_b64decode(p["body"]["data"] + "==").decode("utf-8", "replace")
        for c in p.get("parts") or []:
            t = walk(c)
            if t:
                return t
        return ""
    t = walk(msg.get("payload"))
    return re.sub(r"\r", "", t) if t else (msg.get("snippet") or "")


def addr(s: str) -> str:
    m = re.search(r"<([^>]+)>", s or "")
    return (m.group(1) if m else (s or "")).strip().lower()


def skip_reason(msg: dict, lists: dict, account: str = ME) -> str | None:
    labels = set(msg.get("labelIds") or [])
    if labels & SKIP_LABELS or "INBOX" not in labels:
        return "not an inbox message" if "INBOX" not in labels else "promotional/social"
    frm = addr(header(msg, "From"))
    if frm == account or frm == ME or (AGENT and frm == AGENT) or any(frm.endswith("@" + d) for d in OWN_DOMAINS):
        return "from us"
    dom = frm.split("@")[-1]
    if any(dom == d or dom.endswith("." + d) for d in lists.get("SKIP_DOMAINS", set()) | lists.get("SKIP_SENDERS", set())):
        return f"skip domain {dom}"
    if re.match(r"^(no-?reply|noreply|donotreply|notifications?|mailer-daemon)@", frm):
        return "no-reply sender"
    subj = header(msg, "Subject").lower()
    # Recruiting/job mail is never skipped on a keyword (email_check.py's own override list).
    if re.search(r"\b(interview|offer|hire|hiring|recruit\w*|opportunity|next steps?)\b", subj):
        return None
    # Whole-word match: a substring test skipped every "Salesforce" subject on "sale" (2026-10-22).
    for k in lists.get("SKIP_KEYWORDS", []):
        if re.search(r"(?<![a-z0-9])" + re.escape(k.lower()) + r"(?![a-z0-9])", subj):
            return f"skip keyword {k!r} in subject"
    return None


def free_windows(token: str, now: datetime, days: int = DAYS_AHEAD, cap: int = 12) -> list[str]:
    """The owner's free windows (>= MIN_WINDOW_MIN) in working hours over the next DAYS_AHEAD
    weekdays, from Google free/busy on their own calendars (not the household's)."""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    body = {"timeMin": start.isoformat(), "timeMax": (start + timedelta(days=days + 1)).isoformat(),
            "timeZone": str(TZ), "items": [{"id": "primary"}]}
    req = urllib.request.Request(FREEBUSY, data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        fb = json.load(r)
    busy = []
    for cal in fb.get("calendars", {}).values():
        for b in cal.get("busy", []):
            busy.append((datetime.fromisoformat(b["start"]).astimezone(TZ), datetime.fromisoformat(b["end"]).astimezone(TZ)))
    busy.sort()
    out = []
    for d in range(days + 1):
        day = start + timedelta(days=d)
        if day.weekday() >= 5:
            continue
        cur = day.replace(hour=HOURS[0], minute=HOURS[1])
        end = day.replace(hour=HOURS[2], minute=HOURS[3])
        if end <= now + timedelta(hours=2):
            continue
        cur = max(cur, now + timedelta(hours=2)) if day.date() == now.date() else cur
        for bs, be in busy:
            if be <= cur or bs >= end:
                continue
            if bs - cur >= timedelta(minutes=MIN_WINDOW_MIN):
                out.append((cur, min(bs, end)))
            cur = max(cur, be)
        if end - cur >= timedelta(minutes=MIN_WINDOW_MIN):
            out.append((cur, end))

    def fmt(t):
        return t.strftime("%-I:%M %p").replace(":00", "")

    return [f"{a.strftime('%a %b %-d')}, {fmt(a)}–{fmt(b)}" for a, b in out[:cap]]


def process(payload: dict) -> dict | None:
    # Pub/Sub envelope -> {"emailAddress", "historyId"}; tests may pass the inner dict directly.
    inner = payload
    try:
        data = ((payload.get("message") or {}).get("data"))
        if data:
            inner = json.loads(base64.b64decode(data + "==").decode("utf-8"))
    except Exception:
        inner = payload
    account = (inner.get("emailAddress") or ME).lower()
    if not DATA:
        raise Failed("no state directory (set GMAIL_DELTA_DATA or HOOKSPOOL_STATE_DIR)")
    if account not in STATES:
        log(f"push for unknown account {account}; ignored")
        return None
    STATE_P = STATES[account]
    os.makedirs(DATA, exist_ok=True)
    state = json.load(open(STATE_P, encoding="utf-8")) if os.path.exists(STATE_P) else {}
    token = token_for(account)
    now = datetime.now(TZ)
    lists = _lists()

    def bootstrap(reason: str) -> None:
        prof = gget(token, "profile")
        _g._save(STATE_P, {"historyId": str(prof["historyId"]), "handled": state.get("handled") or [],
                           "bootstrapped": now.isoformat()})
        log(f"bootstrap {account} ({reason}) historyId={prof['historyId']}")

    if not state.get("historyId"):
        bootstrap("no history id")
        return None
    try:
        ids, page, newest = [], None, state["historyId"]
        for _ in range(20):
            p = {"startHistoryId": state["historyId"], "historyTypes": "messageAdded", "labelId": "INBOX", "maxResults": 500}
            if page:
                p["pageToken"] = page
            h = gget(token, "history", p)
            newest = h.get("historyId", newest)
            for rec in h.get("history") or []:
                for a in rec.get("messagesAdded") or []:
                    mid = (a.get("message") or {}).get("id")
                    if mid and mid not in ids:
                        ids.append(mid)
            page = h.get("nextPageToken")
            if not page:
                break
    except urllib.error.HTTPError as e:
        if e.code == 404:                              # history id too old: start over quietly
            bootstrap("history 404")
            return None
        raise Failed(f"history.list HTTP {e.code}: {e.read()[:200]!r}")

    handled = state.get("handled") or []
    hits = []
    seen_now = 0
    for mid in ids:
        if mid in handled:
            continue
        try:
            msg = gget(token, f"messages/{mid}", {"format": "full"})
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise
        seen_now += 1
        if sched and AGENT and account == AGENT and "INBOX" in (msg.get("labelIds") or []):
            sc = sched.classify(msg, header, body_text)
            if sc:                                     # a scheduling hand-off step: never the generic path
                info = sched.load().get(sc["thread_id"], {})
                frm = header(msg, "From")
                hits.append({
                    "event_type": "agent-schedule", "kind": sc["kind"], "account": account, "source": "gmail",
                    "message_id": mid, "thread_id": msg.get("threadId"), "tracked_thread_id": sc["thread_id"],
                    "rfc_message_id": header(msg, "Message-ID"), "references": header(msg, "References"),
                    "from": frm, "subject": header(msg, "Subject"),
                    "meeting_subject": info.get("subject", ""), "others": ", ".join(info.get("others") or []),
                    "ref_tag": sched.ref_tag(sc["thread_id"]),
                    "received": datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, TZ).strftime("%a %b %-d %-I:%M %p"),
                    "new_text": (sc.get("text") or "")[:3000],
                })
                continue
        if account == ME and AGENT and AGENT in (header(msg, "To") + header(msg, "Cc")).lower():
            continue                                   # the agent is on this thread: it handles it, no draft here
        why = skip_reason(msg, lists, account)
        if why:
            continue
        subj, text = header(msg, "Subject"), body_text(msg)
        if account == ME and not SCHEDULING.search(subj + "\n" + text[:4000]):
            continue
        frm = header(msg, "From")
        hits.append({
            "event_type": "scheduling" if account == ME else "agent-mail", "account": account, "source": "gmail", "message_id": mid, "thread_id": msg.get("threadId"),
            "rfc_message_id": header(msg, "Message-ID"), "from": frm, "from_address": addr(frm),
            "from_domain": addr(frm).split("@")[-1], "subject": subj,
            "received": datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, TZ).strftime("%a %b %-d %-I:%M %p"),
            "excerpt": text[:1500].strip(),
        })
    handled = (handled + ids)[-300:]
    _g._save(STATE_P, {"historyId": str(newest), "handled": handled, "bootstrapped": state.get("bootstrapped")})
    if not hits:
        log(f"push {account} historyId={inner.get('historyId', '?')} new={len(ids)} looked={seen_now} -> silent")
        return None
    if account != ME:
        sched_hits = [h for h in hits if h["event_type"] == "agent-schedule"]
        if sched_hits:
            wt = "\n".join("- " + w for w in free_windows(access_token(), now, days=14, cap=20)) or "- (no free working-hours window in the next two weeks)"
            for h in sched_hits:
                h["free_windows_text"] = wt
        log(f"push {account} new={len(ids)} -> {len(hits)} message(s) for the agent"
            + (f" ({len(sched_hits)} scheduling: {', '.join(h['kind'] for h in sched_hits)})" if sched_hits else ""))
        return sched_hits + [h for h in hits if h["event_type"] != "agent-schedule"]
    windows = free_windows(access_token(), now)
    text = "\n".join("- " + w for w in windows) or "- (no free working-hours window in the next week)"
    for hit in hits:                                   # every request is reported: a coalesced batch can hold several
        hit["free_windows_text"] = text
    log(f"push historyId={inner.get('historyId', '?')} new={len(ids)} -> {len(hits)} scheduling request(s), first from "
        f"{hits[0]['from_domain']} ({hits[0]['subject'][:50]!r}); {len(windows)} free windows")
    return hits


def _emit_key(item: dict) -> str:
    return f"{item.get('account')}:{item.get('tracked_thread_id') or item.get('thread_id') or item.get('message_id')}"


def batch_main() -> int:
    """``--batch``: a hookspool batch on stdin; results as ``{"enqueue": ...}`` lines on stdout."""
    try:
        doc = hb.read_document()
        routes = hb.destinations()
        did = hb.delivery_id(doc)

        def work() -> list[dict]:
            latest: dict[str, dict] = {}                  # one pass per mailbox: history is read from the stored id
            for p in hb.events(doc):
                latest[str(p.get("emailAddress") or ME).lower()] = p
            items = []
            for p in latest.values():
                items += process(p) or []
            return [hb.make_emit("gmail", _emit_key(item), item, i, did, routes) for i, item in enumerate(items)]
        return hb.run_batch(doc, work, _write_json, default_state_dir=DATA)
    except (Failed, hb.BatchError) as e:
        log(f"ERROR {e}")
        sys.stderr.write(f"gmail_delta: {e}\n")
        return 1


if __name__ == "__main__":
    if "--batch" in sys.argv[1:]:
        sys.exit(batch_main())
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    try:
        out = process(payload)
    except Failed as e:
        log(f"ERROR {e}")
        sys.stderr.write(f"gmail_delta: {e}\n")
        sys.exit(1)
    print(json.dumps(out, indent=1) if out else "[SILENT]")
