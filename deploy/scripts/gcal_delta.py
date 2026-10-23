#!/usr/bin/env python3
"""Google Calendar push -> routine work for a calendar, wake the agent only for a judgement call.

Run by the hookspool ``script:gcal_delta`` destination (``gcal_delta.py --batch``) with a batch of
push notifications on stdin; also runnable by hand: ``echo '{"resource_state":"exists"}' |
python3 gcal_delta.py`` prints the payload list or "[SILENT]".

What it does, with no LLM:
  1. Pull only the events that changed since the last push (Calendar incremental sync) and
     drop noise: past events, reminder/colour/description-only edits, Google's own
     birthday/working-location/focus entries, and the travel blocks this script creates.
  2. Conflicts: check the affected days against the primary calendar (overlapping timed events).
  3. Travel (optional): for a new/moved event with a real address, run a travel-check script for
     that day and create the travel block it says fits (title "<car> Travel -> <event>"). Gaps too
     short for the computed travel time are a conflict.
  4. Refresh an event store in the background (optional).
  5. Return a payload for the agent ONLY when a conflict needs a choice or a meeting needs a brief;
     otherwise None.

Batch mode (``--batch``): the document's events are pushes for one calendar; the newest (a ``sync``
ping only if nothing else is there) drives one pass. Results leave as ``{"enqueue": ...}`` lines,
state writes are staged and applied only after the journal in ``$HOOKSPOOL_STATE_DIR`` records them
(``hookspool_batch.py``), so a retry of the batch never loses a result and never advances state twice.
Run it with ``max_inflight = 1`` on its destination: the file lock is released before the staged state is applied.

Configuration is by environment (the destination's ``env_file``); nothing deployment-specific is in
this file:
  GCAL_DELTA_CRED        OAuth credential JSON (client_id, client_secret, refresh_token, token_uri); required
  GCAL_DELTA_DATA        state directory (default $HOOKSPOOL_STATE_DIR): sync token, log, actions, brief files
  GCAL_DELTA_TZ          time zone for windows and titles (default UTC)
  GCAL_DELTA_OWN         comma list of addresses that are "us", not external attendees
  GCAL_DELTA_SYNC_SH     optional script started after a routine (event store refresh)
  GCAL_DELTA_SKILL_DIR, GCAL_DELTA_TRAVEL_PY   optional travel check (scripts/travel_check.py run in SKILL_DIR)
  GCAL_DELTA_BRIEF_DIR   optional: where meeting briefs are written (enables brief requests)
  GCAL_DELTA_API         Calendar events URL (default: the primary calendar; for tests)
  HOOKSPOOL_DELTA_ROUTES JSON event_type -> destination, e.g. {"conflict": "hermes:calendar", "brief": "hermes:brief"}

State: $GCAL_DELTA_DATA/gcal_push_state.json (sync token + last-seen signatures).
Log:   gcal_push.log (one line per push, no event details).  Done: push_actions.jsonl.
"""
import fcntl
import re
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hookspool_batch as hb  # noqa: E402

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


CRED = _env("GCAL_DELTA_CRED")
DATA = _env("GCAL_DELTA_DATA") or _env("HOOKSPOOL_STATE_DIR")
STATE = os.path.join(DATA, "gcal_push_state.json")
LOG = os.path.join(DATA, "gcal_push.log")
ACTIONS = os.path.join(DATA, "push_actions.jsonl")
SYNC_SH = _env("GCAL_DELTA_SYNC_SH")
SKILL_DIR = _env("GCAL_DELTA_SKILL_DIR")
TRAVEL_PY = _env("GCAL_DELTA_TRAVEL_PY")
API = _env("GCAL_DELTA_API", "https://www.googleapis.com/calendar/v3/calendars/primary/events")
TZ = ZoneInfo(_env("GCAL_DELTA_TZ", "UTC"))
AHEAD_DAYS = 14
BACK_HOURS = 2
SKIP_TYPES = {"birthday", "workingLocation", "outOfOffice", "focusTime"}
TRAVEL_MARKERS = ("travel", "\U0001f697", "\U0001f687", "\U0001f6b6", "\U0001f6b2")
MODE_EMOJI = {"driving": "\U0001f697", "transit": "\U0001f687", "walking": "\U0001f6b6", "bicycling": "\U0001f6b2"}
PLACEHOLDERS = {"", "null", "none", "n/a", "na", "tbd", "tba", "-", "online", "virtual", "zoom", "google meet"}
SIG_FIELDS = ("start", "end", "summary", "location", "status", "attendees", "conf")
# People whose presence is not "external": the owner, the household, the agents.
OWN = {a.strip().lower() for a in _env("GCAL_DELTA_OWN").split(",") if a.strip()}
PREP_WORDS = re.compile(r"\b(review|prep|preparation|presentation|briefing|interview|pitch|demo|proposal|debrief|"
                        r"kickoff|onboarding|performance|evaluation|assessment|report|workshop|panel|keynote)\b", re.I)


def plain(html: str) -> str:
    """Invite descriptions are often HTML; keep the text, drop the markup."""
    t = re.sub(r"<br\s*/?>|</(p|li|div)>", "\n", html, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t).replace("&amp;", "&").replace("&nbsp;", " ")
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def log(line: str) -> None:
    if not DATA:
        return
    os.makedirs(DATA, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')} {line}\n")


def record(kind: str, **fields) -> None:
    if not DATA:
        return
    with open(ACTIONS, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now(TZ).isoformat(), "kind": kind, **fields}) + "\n")


class Failed(RuntimeError):
    pass


# ---------------------------------------------------------------- credentials
def _expired(data: dict) -> bool:
    exp = data.get("expiry")
    if exp in (None, ""):
        return True
    try:
        if isinstance(exp, (int, float)):
            t = float(exp)
        else:
            d = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            t = d.timestamp()
    except Exception:
        return True
    return t - time.time() < 60


def _write_json(path: str, obj, keep_mode: bool = False) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    mode = os.stat(path).st_mode & 0o777 if keep_mode and os.path.exists(path) else 0o600
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _save(path: str, obj) -> None:
    """State write; staged (not made) while a batch runs, see hookspool_batch.py."""
    hb.save(path, obj, _write_json)


def access_token() -> str:
    if not CRED:
        raise Failed("GCAL_DELTA_CRED is not set")
    try:
        with open(CRED, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise Failed(f"cannot read the credential file: {e.__class__.__name__}")
    if not _expired(data) and (data.get("access_token") or data.get("token")):
        return data.get("access_token") or data["token"]
    body = urllib.parse.urlencode({
        "client_id": data["client_id"], "client_secret": data["client_secret"],
        "refresh_token": data["refresh_token"], "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(data.get("token_uri") or "https://oauth2.googleapis.com/token",
                                 data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            tok = json.load(r)
    except urllib.error.HTTPError as e:
        raise Failed(f"token refresh HTTP {e.code}: {e.read()[:200]!r}")
    except Exception as e:
        raise Failed(f"token refresh failed: {e.__class__.__name__}: {e}")
    data["access_token"] = data["token"] = tok["access_token"]
    exp = datetime.now(timezone.utc) + timedelta(seconds=int(tok.get("expires_in", 3600)))
    data["expiry"] = exp.strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"   # google_auth.py's format
    _write_json(CRED, data, keep_mode=True)
    return tok["access_token"]


# ---------------------------------------------------------------- calendar API
def api(token: str, method: str = "GET", params: dict | None = None, body: dict | None = None, path: str = ""):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read()
        return json.loads(raw) if raw.strip() else {}      # DELETE answers 204 with no body


def list_all(token: str, params: dict) -> tuple[list, str]:
    items, page, sync = [], None, ""
    for _ in range(40):
        p = dict(params)
        if page:
            p["pageToken"] = page
        d = api(token, params=p)
        items.extend(d.get("items") or [])
        page = d.get("nextPageToken")
        if not page:
            sync = d.get("nextSyncToken", "")
            break
    return items, sync


# ---------------------------------------------------------------- event shaping
def when(ev: dict, key: str):
    v = ev.get(key) or {}
    try:
        if v.get("dateTime"):
            return datetime.fromisoformat(v["dateTime"].replace("Z", "+00:00")).astimezone(TZ)
        if v.get("date"):
            return datetime.strptime(v["date"], "%Y-%m-%d").replace(tzinfo=TZ)
    except Exception:
        return None
    return None


def signature(ev: dict) -> dict:
    s, e = when(ev, "start"), when(ev, "end")
    return {
        "start": s.isoformat() if s else None, "end": e.isoformat() if e else None,
        "summary": (ev.get("summary") or "").strip(), "location": (ev.get("location") or "").strip(),
        "status": ev.get("status") or "", "attendees": len(ev.get("attendees") or []),
        "conf": bool(ev.get("hangoutLink") or ev.get("conferenceData")),
        "all_day": bool((ev.get("start") or {}).get("date")),
        "link": ev.get("htmlLink") or "", "organizer": ((ev.get("organizer") or {}).get("email") or ""),
        # Outside people: attendees, plus the creator (scheduling tools such as GoodTime invite
        # from a group calendar and name the interviewer only in the description).
        "external": sorted({e for e in [(a.get("email") or "").lower() for a in (ev.get("attendees") or [])
                                        if not a.get("resource")] + [((ev.get("creator") or {}).get("email") or "").lower()]
                            if e and e not in OWN and "@group.calendar.google.com" not in e}),
        "desc": plain(ev.get("description") or "")[:1500],
    }


def has_address(sig: dict) -> bool:
    loc = (sig.get("location") or "").strip().lower()
    return loc not in PLACEHOLDERS and len(loc) > 5


def is_travel(sig: dict) -> bool:
    return any(m in (sig.get("summary") or "").lower() for m in TRAVEL_MARKERS)


def in_window(sig: dict, now: datetime) -> bool:
    if not sig or not sig.get("start"):
        return False
    s = datetime.fromisoformat(sig["start"])
    return now - timedelta(hours=BACK_HOURS) <= s <= now + timedelta(days=AHEAD_DAYS)


def fmt(sig: dict) -> str:
    if not sig or not sig.get("start"):
        return "(no time)"
    s = datetime.fromisoformat(sig["start"])
    if sig.get("all_day"):
        return s.strftime("%a %b %-d (all day)")
    e = datetime.fromisoformat(sig["end"]) if sig.get("end") else None
    t = s.strftime("%a %b %-d %-I:%M %p")
    if e and e.date() == s.date():
        t += "–" + e.strftime("%-I:%M %p")
    return t


def describe(kind: str, sig: dict, prev: dict | None) -> str:
    title = f'"{sig.get("summary") or "(untitled)"}"'
    loc = f"  at {sig['location']}" if sig.get("location") else ""
    if kind == "new":
        return f"NEW  {fmt(sig)}  {title}{loc}"
    if kind == "cancelled":
        return f"CANCELLED  {fmt(prev or sig)}  {title}"
    if kind == "moved":
        return f"MOVED  {title}  {fmt(prev)} → {fmt(sig)}{loc}"
    bits = []
    if prev and prev.get("summary") != sig.get("summary"):
        bits.append(f'title: "{prev.get("summary")}" → "{sig.get("summary")}"')
    if prev and prev.get("location") != sig.get("location"):
        bits.append(f"location: {prev.get('location') or '(none)'} → {sig.get('location') or '(none)'}")
    if prev and prev.get("attendees") != sig.get("attendees"):
        bits.append(f"attendees {prev.get('attendees')} → {sig.get('attendees')}")
    if prev and prev.get("conf") != sig.get("conf"):
        bits.append("video link " + ("added" if sig.get("conf") else "removed"))
    return f"UPDATED  {fmt(sig)}  {title}  " + "; ".join(bits)


# ---------------------------------------------------------------- conflicts (primary calendar)
def overlaps(a: dict, b: dict) -> bool:
    if a.get("all_day") or b.get("all_day") or not (a.get("start") and a.get("end") and b.get("start") and b.get("end")):
        return False          # all-day events never conflict with timed ones
    return a["start"] < b["end"] and b["start"] < a["end"]


def conflicts_for(changed: list[tuple[str, dict, str]], seen: dict) -> list[str]:
    out = []
    for eid, sig, kind in changed:
        if kind == "cancelled" or sig.get("status") == "cancelled":
            continue
        for oid, other in seen.items():
            if oid == eid or other.get("status") == "cancelled" or is_travel(other):
                continue
            if overlaps(sig, other):
                out.append(f'"{sig.get("summary")}" ({fmt(sig)}) overlaps "{other.get("summary")}" ({fmt(other)})')
    return out


# ---------------------------------------------------------------- travel block upkeep
def drop_travel_blocks_for(token: str, title: str, seen: dict) -> list[str]:
    """Delete the travel blocks this script made that point at `title` (an event that moved or was cancelled),
    so a stale block never survives its event. The caller recreates one if it still fits."""
    done = []
    for eid, sig in list(seen.items()):
        if is_travel(sig) and (sig.get("summary") or "").endswith("Travel \u2192 " + title):
            try:
                api(token, "DELETE", path="/" + eid)
                seen.pop(eid, None)
                done.append(f'removed stale "{sig.get("summary")}" ({fmt(sig)})')
                record("travel_block_removed", event_id=eid, title=sig.get("summary"))
            except Exception as e:
                log(f"travel block delete failed {eid}: {e.__class__.__name__}")
    return done


# ---------------------------------------------------------------- travel (optional travel-check script)
def travel_for_day(token: str, date: str, changed_titles: set[str], day_events: dict) -> tuple[list[str], list[str]]:
    """Run the skill's travel_check.py for one day; create the blocks it says fit when they
    touch a changed event. Returns (actions, conflicts)."""
    actions, conflicts = [], []
    if not (TRAVEL_PY and SKILL_DIR):
        return actions, conflicts                          # travel blocks are an optional feature
    try:
        p = subprocess.run([TRAVEL_PY, "scripts/travel_check.py", "--target", date], cwd=SKILL_DIR,
                           capture_output=True, text=True, timeout=40)
        res = json.loads(p.stdout)
    except subprocess.TimeoutExpired:
        log(f"travel_check timed out for {date}")
        return ["travel check for %s timed out (no block created)" % date], []
    except Exception as e:
        log(f"travel_check failed for {date}: {e.__class__.__name__}: {str(e)[:120]}")
        return ["travel check for %s failed (no block created)" % date], []
    if res.get("degraded"):
        # never fall back to a guess when the Places/Routes side is degraded.
        return [f"travel check for {date}: {res['degraded']} (no block created)"], []
    starts = {e["summary"]: e for e in res.get("events") or []}
    # travel_check pairs events across every household calendar. A block on the primary calendar is
    # only right when BOTH ends of the leg are its own events (it once paired one person's brunch with
    # another's appointment and put a block on the wrong calendar). `day_events` is the primary
    # calendar's view of the day.
    def mine(title):
        ev = starts.get(title)
        return bool(ev) and any((v.get("summary") == title and (v.get("start") or "")[:16] == ev["start"][:16])
                                for v in day_events.values())
    mode = res.get("mode") or "driving"
    legs = list(res.get("pairs") or [])
    fl = res.get("first_leg")
    if isinstance(fl, dict) and fl.get("to"):
        legs.append(dict(fl, **{"from": fl.get("from") or "home"}))
    for leg in legs:
        to = leg.get("to")
        frm = leg.get("from")
        if to not in changed_titles and frm not in changed_titles:
            continue
        if not mine(to) or (leg.get("kind") != "first_leg" and not mine(frm)):
            continue                                  # someone else's appointment
        action = leg.get("action") or ""
        # a block only joins a prior event within 90 min; with a longer gap the person goes home in
        # between and the origin would be wrong.
        gap = leg.get("gap_min")
        if gap is not None and float(gap) > 90:
            continue
        if action == "surface_conflict_no_block_created":
            conflicts.append(f'not enough time to get from "{frm}" to "{to}" on {date}: needs '
                             f'{leg.get("travel_plus_buffer_min")} min, gap is {leg.get("gap_min")} min')
            continue
        if action != "travel_block_would_fit" or not leg.get("travel_plus_buffer_min"):
            continue
        dest = starts.get(to)
        if not dest:
            continue
        title = f"{MODE_EMOJI.get(mode, MODE_EMOJI['driving'])} Travel → {to}"
        if any(is_travel(s) and to in (s.get("summary") or "") for s in day_events.values()):
            continue                                  # a block for this leg already exists
        end = datetime.fromisoformat(dest["start"])
        start = end - timedelta(minutes=float(leg["travel_plus_buffer_min"]))
        body = {"summary": title, "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()},
                "description": f"Travel time from \"{frm}\": {leg.get('computed_minutes')} min + "
                               f"{res.get('buffer_minutes', 10)} min buffer, computed by the travel check.",
                "transparency": "transparent"}
        try:
            ev = api(token, "POST", body=body)
            actions.append(f'created "{title}" {start.strftime("%-I:%M %p")}–{end.strftime("%-I:%M %p")} ({leg["travel_plus_buffer_min"]} min)')
            record("travel_block_created", date=date, title=title, event_id=ev.get("id"), minutes=leg["travel_plus_buffer_min"])
        except Exception as e:
            actions.append(f'could not create "{title}": {e.__class__.__name__}')
            log(f"travel block insert failed: {e}")
    return actions, conflicts



# ---------------------------------------------------------------- meeting brief trigger
def needs_brief(sig: dict, now: datetime) -> bool:
    if not BRIEF_DIR or sig.get("all_day") or sig.get("briefed") or not in_window(sig, now):
        return False
    ext = sig.get("external") or []
    if ext or (sig.get("attendees") or 0) >= 3 or PREP_WORDS.search(sig.get("summary") or ""):
        return True
    return False


BRIEF_DIR = _env("GCAL_DELTA_BRIEF_DIR")        # empty: no meeting briefs
REQUESTED = os.path.join(DATA, "briefs_requested.json")   # read by brief_publish.py --pending
PUBLISHED = os.path.join(DATA, "briefs_published.json")
BRIEF_RETRY_AFTER = timedelta(hours=6)   # the free router resets daily at 17:00 PT
BRIEF_MAX_ATTEMPTS = 4


def _load(path: str) -> dict:
    """The file's JSON object, or what this run has staged for it."""
    st = hb.staged(path)
    if st is not None:
        return st
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}


def mark_requested(eid: str, payload: dict, now: datetime) -> None:
    req = _load(REQUESTED)
    r = req.get(eid) or {"attempts": 0}
    r.update(path=payload["brief_path"], at=now.isoformat(), attempts=r["attempts"] + 1, title=payload.get("title"))
    req[eid] = r
    _save(REQUESTED, req)


def retry_briefs(seen: dict, now: datetime) -> list:
    """A brief run can die (the free model router runs dry): re-request it on a later push."""
    out, published = [], _load(PUBLISHED)
    if not BRIEF_DIR:
        return out
    for eid, r in _load(REQUESTED).items():
        sig = seen.get(eid)
        if (eid in published or not sig or os.path.exists(r.get("path") or "")
                or r.get("attempts", 0) >= BRIEF_MAX_ATTEMPTS
                or now - datetime.fromisoformat(r["at"]) < BRIEF_RETRY_AFTER
                or (sig.get("start") or "") < (now + timedelta(hours=1)).isoformat()):
            continue
        sig = dict(sig, _id=eid)
        p = brief_for(sig, now)
        mark_requested(eid, p, now)
        record("brief_retry", event_id=eid, attempt=r.get("attempts", 0) + 1)
        out.append(p)
    return out


def brief_for(sig: dict, now: datetime) -> dict:
    s = datetime.fromisoformat(sig["start"])
    e = datetime.fromisoformat(sig["end"]) if sig.get("end") else None
    mins = int((e - s).total_seconds() // 60) if e else 0
    slug = re.sub(r"[^a-z0-9]+", "-", (sig.get("summary") or "meeting").lower()).strip("-")[:40]
    return {"event_type": "brief", "source": "google-calendar", "title": sig.get("summary"),
            "when": fmt(sig), "duration_min": mins, "organizer": sig.get("organizer"),
            "attendees_text": ", ".join(sig.get("external") or []) or "(none external)",
            "description": (sig.get("desc") or "").strip() or "(none)", "location": sig.get("location") or "",
            "link": sig.get("link") or "", "brief_path": f"{BRIEF_DIR}/{s.strftime('%Y-%m-%d')}-{slug}-{(sig.get('_id') or 'x')[:8]}.md",
            "event_id": sig.get("_id", "")}

# ---------------------------------------------------------------- main
def process(payload: dict) -> dict | None:
    """Handle one push. Returns a list of Hermes payloads (conflict and/or brief) or None."""
    state_ev = str(payload.get("resource_state") or payload.get("event_type") or "")
    msg_no = payload.get("message_number", "")
    if not DATA:
        raise Failed("no state directory (set GCAL_DELTA_DATA or HOOKSPOOL_STATE_DIR)")
    os.makedirs(DATA, exist_ok=True)
    lock = open(STATE + ".lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        state = json.load(open(STATE, encoding="utf-8")) if os.path.exists(STATE) else {}
        token = access_token()
        now = datetime.now(TZ)
        seen: dict = state.get("last_seen") or {}

        def bootstrap(reason: str) -> None:
            items, sync = list_all(token, {"timeMin": (now - timedelta(days=1)).isoformat(),
                                           "singleEvents": "true", "showDeleted": "true", "maxResults": 250})
            if not sync:
                raise Failed("bootstrap returned no sync token")
            fresh = {ev["id"]: signature(ev) for ev in items
                     if ev.get("status") != "cancelled" and ev.get("eventType") not in SKIP_TYPES}
            _save(STATE, {"syncToken": sync, "last_seen": fresh, "bootstrapped": now.isoformat()})
            log(f"bootstrap ({reason}) state={state_ev} msg={msg_no} tracked={len(fresh)}")

        if state_ev == "sync" or not state.get("syncToken"):
            bootstrap("sync message" if state_ev == "sync" else "no sync token")
            return None
        try:
            items, sync = list_all(token, {"syncToken": state["syncToken"], "singleEvents": "true",
                                           "showDeleted": "true", "maxResults": 250})
        except urllib.error.HTTPError as e:
            if e.code == 410:
                bootstrap("410 Gone")
                return None
            raise Failed(f"events.list HTTP {e.code}: {e.read()[:200]!r}")

        changes = []                                  # (kind, sig, prev, id)
        for ev in items:
            eid = ev.get("id")
            if not eid:
                continue
            prev = seen.get(eid)
            if ev.get("status") == "cancelled":
                if prev and in_window(prev, now) and not is_travel(prev):
                    changes.append(("cancelled", prev, prev, eid))
                seen.pop(eid, None)
                continue
            if ev.get("eventType") in SKIP_TYPES:
                continue
            sig = signature(ev)
            if is_travel(sig):
                seen[eid] = sig                       # our own travel blocks: tracked, never a trigger
                continue
            if prev is None:
                if in_window(sig, now):
                    changes.append(("new", sig, None, eid))
            else:
                diff = [k for k in SIG_FIELDS if prev.get(k) != sig.get(k)]
                if diff and (in_window(sig, now) or in_window(prev, now)):
                    changes.append(("moved" if ("start" in diff or "end" in diff) else "updated", sig, prev, eid))
            if prev and prev.get("briefed"):
                sig["briefed"] = True
            seen[eid] = sig
        cutoff = (now - timedelta(days=1)).isoformat()
        seen = {k: v for k, v in seen.items() if (v.get("start") or "") >= cutoff}
        _save(STATE, {"syncToken": sync or state["syncToken"], "last_seen": seen,
                      "bootstrapped": state.get("bootstrapped")})
        retries = retry_briefs(seen, now)
        if not changes:
            log(f"ping state={state_ev} msg={msg_no} items={len(items)} -> silent"
                + (f"; brief retry x{len(retries)}" if retries else ""))
            return retries or None

        # --- the routine -------------------------------------------------------
        lines = ["- " + describe(k, s, p) for k, s, p, _ in changes]
        live = [(eid, s, k) for k, s, p, eid in changes if k != "cancelled"]
        conflicts = conflicts_for(live, seen)
        actions: list[str] = []
        for k, s_, p_, _ in changes:
            if k in ("moved", "cancelled") and (p_ or {}).get("summary"):
                actions += drop_travel_blocks_for(token, p_["summary"], seen)
        dates = sorted({(s.get("start") or "")[:10] for k, s, p, _ in changes for s in ((s,) if k != "cancelled" else (p,)) if s and s.get("start")})
        for date in dates:
            titles = {s.get("summary") for eid, s, k in live if (s.get("start") or "")[:10] == date
                      and k in ("new", "moved", "updated") and has_address(s) and not s.get("all_day")}
            if not titles:
                continue
            day_events = {k: v for k, v in seen.items() if (v.get("start") or "")[:10] == date}
            a, c = travel_for_day(token, date, titles, day_events)
            actions += a
            conflicts += c
        if SYNC_SH:
            try:
                subprocess.Popen(["bash", SYNC_SH], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                actions.append("event store refresh started")
            except Exception as e:
                log(f"events-sync spawn failed: {e}")
        log(f"ping state={state_ev} msg={msg_no} items={len(items)} -> {len(changes)} change(s): "
            + ", ".join(k for k, *_ in changes) + f"; conflicts={len(conflicts)}; actions={len(actions)}")
        record("push", changes=[k for k, *_ in changes], conflicts=conflicts, actions=actions)
        out = []
        if conflicts:
            out.append({"source": "google-calendar", "event_type": "conflict", "resource_state": state_ev, "count": len(changes),
                        "dates_text": ", ".join(dates), "summary": "\n".join(lines),
                        "conflicts_text": "\n".join("- " + c for c in conflicts),
                        "actions_text": "; ".join(actions) or "nothing"})
        briefs = 0
        for eid, s, k in live:
            if k in ("new", "moved") and needs_brief(s, now):
                s["_id"] = eid
                bp = brief_for(s, now)
                mark_requested(eid, bp, now)
                out.append(bp)
                briefs += 1
                seen[eid]["briefed"] = True
                record("brief_requested", event_id=eid, title=s.get("summary"), attendees=s.get("external"))
        if briefs:
            _save(STATE, {"syncToken": sync or state["syncToken"], "last_seen": seen,
                          "bootstrapped": state.get("bootstrapped")})
            log(f"brief requested for {briefs} event(s)")
        out += retries
        return out or None
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _emit_key(item: dict) -> str:
    return f"brief:{item.get('event_id') or 'x'}" if item.get("event_type") == "brief" else str(item.get("event_type"))


def batch_main() -> int:
    """``--batch``: a hookspool batch on stdin; results as ``{"enqueue": ...}`` lines on stdout."""
    try:
        doc = hb.read_document()
        routes = hb.destinations()
        did = hb.delivery_id(doc)

        def work() -> list[dict]:
            pushes = hb.events(doc)
            real = [p for p in pushes if str(p.get("resource_state") or p.get("event_type")) != "sync"]
            chosen = (real or pushes or [{}])[-1]            # one pass reads every change since the sync token
            return [hb.make_emit("gcal", _emit_key(item), item, i, did, routes)
                    for i, item in enumerate(process(chosen) or [])]
        return hb.run_batch(doc, work, _write_json, default_state_dir=DATA)
    except (Failed, hb.BatchError) as e:
        log(f"ERROR {e}")
        sys.stderr.write(f"gcal_delta: {e}\n")
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
        sys.stderr.write(f"gcal_delta: {e}\n")
        sys.exit(1)
    print(json.dumps(out, indent=1) if out else "[SILENT]")
