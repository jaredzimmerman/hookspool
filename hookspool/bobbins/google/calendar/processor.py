"""Google Calendar processor: a push for a calendar -> routine work, and the agent only for a judgement call.

Runs for one batch of watch-channel pushes for one calendar; the newest real push (a ``sync`` ping only
when nothing else is there) drives one pass. No model is involved. What a pass does:

1. Pull only the events that changed since the last pass (Calendar incremental sync) and drop noise: past
   events, reminder/colour/description-only edits, Google's own birthday/working-location/focus entries and
   the travel blocks this bobbin creates.
2. Conflicts: check the affected days against the primary calendar (overlapping timed events).
3. Travel (optional): for a new or moved event with a real address, ask the deployment's hook
   (``hooks.travel``, README.md) what the day needs; create the travel block it says fits, titled
   "<mode symbol> Travel -> <event>". A gap too short for the travel time is a conflict. A moved or cancelled
   event takes the travel blocks this bobbin made for it with it.
4. Report. ``conflict`` when a conflict needs a choice; ``brief`` when a meeting needs a brief (only when the
   ``brief_dir`` setting is set); ``changed`` once per pass that saw real changes, carrying the changed event
   ids and dates, so a deployment can run its own follow-up (an event-store refresh) from a durable
   ``script:`` destination.

State: ``gcal_push_state`` ``{syncToken, last_seen: {event id: signature}, bootstrapped}`` and, when briefs
are on, ``briefs_requested`` (written here) and ``briefs_published`` (written by whatever publishes the
briefs; only read here). The runtime stages every write and applies it after the journal has the result, so a
retried batch never loses a report and never advances the sync token twice. Google calls go through
``hookspool.providers.google`` on ``ctx.oauth``.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from os.path import exists as path_exists
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hookspool import net
from hookspool.providers import google as G

OUTPUTS = ("conflict", "brief", "changed")             # the manifest's [outputs]
STATE, REQUESTED, PUBLISHED = "gcal_push_state", "briefs_requested", "briefs_published"
SOURCE = "google-calendar"
AHEAD_DAYS = 14
BACK_HOURS = 2
SKIP_TYPES = {"birthday", "workingLocation", "outOfOffice", "focusTime"}
TRAVEL_MARKERS = ("travel", "\U0001f697", "\U0001f687", "\U0001f6b6", "\U0001f6b2")
MODE_EMOJI = {"driving": "\U0001f697", "transit": "\U0001f687", "walking": "\U0001f6b6", "bicycling": "\U0001f6b2"}
PLACEHOLDERS = {"", "null", "none", "n/a", "na", "tbd", "tba", "-", "online", "virtual", "zoom", "google meet"}
SIG_FIELDS = ("start", "end", "summary", "location", "status", "attendees", "conf")
PREP_WORDS = re.compile(r"\b(review|prep|preparation|presentation|briefing|interview|pitch|demo|proposal|debrief|"
                        r"kickoff|onboarding|performance|evaluation|assessment|report|workshop|panel|keynote)\b", re.I)
BRIEF_RETRY_AFTER = timedelta(hours=6)
BRIEF_MAX_ATTEMPTS = 4


class Config:
    def __init__(self, ctx):
        s = ctx.settings
        try:
            self.tz_name = str(s["tz"])
            self.tz = ZoneInfo(self.tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            ctx.fail_permanent("the tz setting is not a time zone")
        # people whose presence is not "external": the owner, the household, the agents
        self.own = {str(a).strip().lower() for a in s["own"] if str(a).strip()}
        self.brief_dir = str(s["brief_dir"]).rstrip("/")
        self.api = str(s["api_url"])


def plain(html: str) -> str:
    """Invite descriptions are often HTML; keep the text, drop the markup."""
    t = re.sub(r"<br\s*/?>|</(p|li|div)>", "\n", html, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t).replace("&amp;", "&").replace("&nbsp;", " ")
    return re.sub(r"\n{3,}", "\n\n", t).strip()


# ---- event shaping ----------------------------------------------------------------------------------------------
def when(ev: dict, key: str, tz):
    v = ev.get(key) or {}
    try:
        if v.get("dateTime"):
            return datetime.fromisoformat(v["dateTime"].replace("Z", "+00:00")).astimezone(tz)
        if v.get("date"):
            return datetime.strptime(v["date"], "%Y-%m-%d").replace(tzinfo=tz)
    except ValueError:
        return None
    return None


def signature(ev: dict, cfg: Config) -> dict:
    s, e = when(ev, "start", cfg.tz), when(ev, "end", cfg.tz)
    return {
        "start": s.isoformat() if s else None, "end": e.isoformat() if e else None,
        "summary": (ev.get("summary") or "").strip(), "location": (ev.get("location") or "").strip(),
        "status": ev.get("status") or "", "attendees": len(ev.get("attendees") or []),
        "conf": bool(ev.get("hangoutLink") or ev.get("conferenceData")),
        "all_day": bool((ev.get("start") or {}).get("date")),
        "link": ev.get("htmlLink") or "", "organizer": ((ev.get("organizer") or {}).get("email") or ""),
        # Outside people: attendees, plus the creator (scheduling tools invite from a group calendar and
        # name the interviewer only in the description).
        "external": sorted({e for e in [(a.get("email") or "").lower() for a in (ev.get("attendees") or [])
                                        if not a.get("resource")] + [((ev.get("creator") or {}).get("email") or "").lower()]
                            if e and e not in cfg.own and "@group.calendar.google.com" not in e}),
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


# ---- conflicts (primary calendar) --------------------------------------------------------------------------------
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


# ---- travel block upkeep -----------------------------------------------------------------------------------------
def drop_travel_blocks_for(ctx, client, cfg: Config, title: str, seen: dict) -> list[str]:
    """Delete the travel blocks this bobbin made that point at ``title`` (an event that moved or was
    cancelled), so a stale block never survives its event. The caller recreates one if it still fits."""
    done = []
    for eid, sig in list(seen.items()):
        if is_travel(sig) and (sig.get("summary") or "").endswith("Travel → " + title):
            try:
                G.calendar_delete(client, eid, url=cfg.api)
                seen.pop(eid, None)
                done.append(f'removed stale "{sig.get("summary")}" ({fmt(sig)})')
            except Exception as e:  # noqa: BLE001 - a block that cannot be removed is reported, not fatal
                ctx.log(f"travel block delete failed: {e.__class__.__name__}")
    return done


def travel_for_event(ctx, client, cfg: Config, eid: str, sig: dict, day_events: dict) -> tuple[list[str], list[str]]:
    """Ask ``hooks.travel(event, day_events, ctx)`` about one changed event and act on its answer.

    It returns ``None`` (nothing to do) or a dict with any of: ``block`` (``{"minutes": N, "from": text,
    "computed_minutes": N, "buffer_minutes": N, "mode": "driving"|"transit"|"walking"|"bicycling"}``: a travel
    block of N minutes ending when the event starts), ``conflict`` (text: not enough time to get there),
    ``note`` (text for the report). Returns (actions, conflicts)."""
    actions: list[str] = []
    conflicts: list[str] = []
    travel = getattr(ctx.hooks, "travel", None) if ctx.hooks is not None else None
    if not callable(travel):
        return actions, conflicts                          # travel blocks are an optional feature
    date = (sig.get("start") or "")[:10]
    try:
        res = travel(dict(sig, id=eid), {k: dict(v) for k, v in day_events.items()}, ctx)
    except Exception as e:  # noqa: BLE001 - deployment code; never a reason to lose the pass
        ctx.log(f"travel hook failed for {date}: {e.__class__.__name__}")
        return [f"travel check for {date} failed (no block created)"], []
    if not isinstance(res, dict):
        return actions, conflicts
    if isinstance(res.get("note"), str) and res["note"]:
        actions.append(res["note"])
    if isinstance(res.get("conflict"), str) and res["conflict"]:
        conflicts.append(res["conflict"])
    blk = res.get("block")
    if not isinstance(blk, dict):
        return actions, conflicts
    try:
        minutes = float(blk.get("minutes"))
    except (TypeError, ValueError):
        return actions, conflicts
    if minutes <= 0:
        return actions, conflicts
    to = sig.get("summary") or ""
    mode = blk.get("mode") if blk.get("mode") in MODE_EMOJI else "driving"
    title = f"{MODE_EMOJI[mode]} Travel → {to}"
    if any(is_travel(s) and to in (s.get("summary") or "") for s in day_events.values()):
        return actions, conflicts                          # a block for this leg already exists
    end = datetime.fromisoformat(sig["start"])
    start = end - timedelta(minutes=minutes)
    body = {"summary": title, "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()},
            "description": f"Travel time from \"{blk.get('from') or 'home'}\": {blk.get('computed_minutes')} min + "
                           f"{blk.get('buffer_minutes', 10)} min buffer, computed by the travel check.",
            "transparency": "transparent"}
    try:
        G.calendar_insert(client, body, url=cfg.api)
        actions.append(f'created "{title}" {start.strftime("%-I:%M %p")}–{end.strftime("%-I:%M %p")} ({blk.get("minutes")} min)')
    except Exception as e:  # noqa: BLE001 - reported, the pass goes on
        actions.append(f'could not create "{title}": {e.__class__.__name__}')
        ctx.log(f"travel block insert failed: {e.__class__.__name__}")
    return actions, conflicts


# ---- meeting briefs ----------------------------------------------------------------------------------------------
def needs_brief(cfg: Config, sig: dict, now: datetime) -> bool:
    if not cfg.brief_dir or sig.get("all_day") or sig.get("briefed") or not in_window(sig, now):
        return False
    return bool(sig.get("external") or (sig.get("attendees") or 0) >= 3 or PREP_WORDS.search(sig.get("summary") or ""))


def brief_for(cfg: Config, sig: dict) -> dict:
    s = datetime.fromisoformat(sig["start"])
    e = datetime.fromisoformat(sig["end"]) if sig.get("end") else None
    mins = int((e - s).total_seconds() // 60) if e else 0
    slug = re.sub(r"[^a-z0-9]+", "-", (sig.get("summary") or "meeting").lower()).strip("-")[:40]
    eid = sig.get("_id", "")
    return {"event_type": "brief", "source": SOURCE, "key": f"brief:{eid or 'x'}", "title": sig.get("summary"),
            "when": fmt(sig), "duration_min": mins, "organizer": sig.get("organizer"),
            "attendees_text": ", ".join(sig.get("external") or []) or "(none external)",
            "description": (sig.get("desc") or "").strip() or "(none)", "location": sig.get("location") or "",
            "link": sig.get("link") or "",
            "brief_path": f"{cfg.brief_dir}/{s.strftime('%Y-%m-%d')}-{slug}-{(eid or 'x')[:8]}.md",
            "event_id": eid}


def mark_requested(ctx, eid: str, item: dict, now: datetime) -> None:
    req = ctx.load_state(REQUESTED, {})
    r = req.get(eid) or {"attempts": 0}
    r.update(path=item["brief_path"], at=now.isoformat(), attempts=r["attempts"] + 1, title=item.get("title"))
    req[eid] = r
    ctx.save_state(REQUESTED, req)


def retry_briefs(ctx, cfg: Config, seen: dict, now: datetime) -> list[dict]:
    """A brief run can die (the model router runs dry): re-request it on a later push."""
    out: list[dict] = []
    if not cfg.brief_dir:
        return out
    published = ctx.load_state(PUBLISHED, {})
    for eid, r in ctx.load_state(REQUESTED, {}).items():
        sig = seen.get(eid)
        if (eid in published or not sig or path_exists(r.get("path") or "")
                or r.get("attempts", 0) >= BRIEF_MAX_ATTEMPTS
                or now - datetime.fromisoformat(r["at"]) < BRIEF_RETRY_AFTER
                or (sig.get("start") or "") < (now + timedelta(hours=1)).isoformat()):
            continue
        item = brief_for(cfg, dict(sig, _id=eid))
        mark_requested(ctx, eid, item, now)
        ctx.log(f"brief retry attempt {r.get('attempts', 0) + 1}")
        out.append(item)
    return out


# ---- one pass ---------------------------------------------------------------------------------------------------
def _pass(ctx, cfg: Config, push: dict) -> list[dict]:
    state_ev = str(push.get("resource_state") or push.get("event_type") or "")
    client = ctx.oauth("cred")
    state = ctx.load_state(STATE, {})
    now = datetime.fromtimestamp(ctx.now(), cfg.tz)
    seen: dict = state.get("last_seen") or {}

    def bootstrap(reason: str) -> None:
        items, sync = G.calendar_events(client, {"timeMin": (now - timedelta(days=1)).isoformat(),
                                                 "singleEvents": "true", "showDeleted": "true", "maxResults": 250},
                                        url=cfg.api)
        if not sync:
            raise net.Retryable("calendar bootstrap returned no sync token")
        fresh = {ev["id"]: signature(ev, cfg) for ev in items
                 if ev.get("status") != "cancelled" and ev.get("eventType") not in SKIP_TYPES}
        ctx.save_state(STATE, {"syncToken": sync, "last_seen": fresh, "bootstrapped": now.isoformat()})
        ctx.log(f"bootstrap ({reason}) state={state_ev} tracked={len(fresh)}")

    if state_ev == "sync" or not state.get("syncToken"):
        bootstrap("sync message" if state_ev == "sync" else "no sync token")
        return []
    try:
        items, sync = G.calendar_events(client, {"syncToken": state["syncToken"], "singleEvents": "true",
                                                 "showDeleted": "true", "maxResults": 250}, url=cfg.api)
    except G.SyncGone:
        bootstrap("410 Gone")
        return []

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
        sig = signature(ev, cfg)
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
    ctx.save_state(STATE, {"syncToken": sync or state["syncToken"], "last_seen": seen,
                           "bootstrapped": state.get("bootstrapped")})
    retries = retry_briefs(ctx, cfg, seen, now)
    if not changes:
        ctx.log(f"ping state={state_ev} items={len(items)} -> silent" + (f"; brief retry x{len(retries)}" if retries else ""))
        return retries

    # --- the routine ---------------------------------------------------------------------------------------------
    lines = ["- " + describe(k, s, p) for k, s, p, _ in changes]
    live = [(eid, s, k) for k, s, p, eid in changes if k != "cancelled"]
    conflicts = conflicts_for(live, seen)
    actions: list[str] = []
    for k, s_, p_, _ in changes:
        if k in ("moved", "cancelled") and (p_ or {}).get("summary"):
            actions += drop_travel_blocks_for(ctx, client, cfg, p_["summary"], seen)
    dates = sorted({(s.get("start") or "")[:10] for k, s, p, _ in changes for s in ((s,) if k != "cancelled" else (p,))
                    if s and s.get("start")})
    for date in dates:
        day_events = {k: v for k, v in seen.items() if (v.get("start") or "")[:10] == date}
        for eid, s, k in live:
            if (s.get("start") or "")[:10] == date and k in ("new", "moved", "updated") and has_address(s) \
                    and not s.get("all_day"):
                a, c = travel_for_event(ctx, client, cfg, eid, s, day_events)
                actions += a
                conflicts += c
    ctx.log(f"ping state={state_ev} items={len(items)} -> {len(changes)} change(s): "
            + ", ".join(k for k, *_ in changes) + f"; conflicts={len(conflicts)}; actions={len(actions)}")
    out: list[dict] = []
    if conflicts:
        out.append({"source": SOURCE, "event_type": "conflict", "key": "conflict", "resource_state": state_ev,
                    "count": len(changes), "dates_text": ", ".join(dates), "summary": "\n".join(lines),
                    "conflicts_text": "\n".join("- " + c for c in conflicts),
                    "actions_text": "; ".join(actions) or "nothing"})
    briefs = 0
    for eid, s, k in live:
        if k in ("new", "moved") and needs_brief(cfg, s, now):
            item = brief_for(cfg, dict(s, _id=eid))
            mark_requested(ctx, eid, item, now)
            out.append(item)
            briefs += 1
            seen[eid]["briefed"] = True
    if briefs:
        ctx.save_state(STATE, {"syncToken": sync or state["syncToken"], "last_seen": seen,
                               "bootstrapped": state.get("bootstrapped")})
        ctx.log(f"brief requested for {briefs} event(s)")
    out += retries
    out.append({"source": SOURCE, "event_type": "changed", "key": "changed", "resource_state": state_ev,
                "count": len(changes), "kinds": [k for k, *_ in changes],
                "event_ids": list(dict.fromkeys(eid for *_, eid in changes)), "dates": dates})
    return out


def process(batch, ctx) -> list[dict]:
    cfg = Config(ctx)
    pushes = batch.events
    real = [p for p in pushes if str(p.get("resource_state") or p.get("event_type")) != "sync"]
    chosen = (real or pushes or [{}])[-1]               # one pass reads every change since the sync token
    return _pass(ctx, cfg, chosen)
