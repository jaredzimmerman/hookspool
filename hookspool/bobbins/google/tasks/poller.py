"""Google Tasks poller: the Tasks API has no push, so every tick asks each followed list what changed.

For each followed task list one ``tasks.list`` call (``updatedMin`` = the newest ``updated`` seen in that list minus
a one-minute margin, completed, hidden and deleted tasks included) returns the tasks that changed since the last
pass. No model is involved and no task text is read unless ``include_titles`` is on. A pass:

1. Lists the account's task lists and keeps those named by ``tasklists`` (by id, or by title ignoring case); an
   empty setting keeps all. Names that match no list are counted in the log (``unmatched=N``).
2. A list the state has not seen yet is only recorded: its newest ``updated`` and the ``{id: updated}`` of the tasks
   inside the margin are stored and nothing is reported, so the first pass (and a list added later) emits no
   history. When the whole list cannot be read in one pass (more than 10,000 retained tasks) the cursor starts at
   the tick time instead, because a partial read of an unordered list says nothing about where the list stands.
3. A list already recorded is read from its cursor minus the margin. The margin catches a write whose ``updated``
   was stamped before a later one but became visible after it; a task whose id and ``updated`` are already in the
   kept map is skipped, every other returned task becomes a ``task-changed`` item.
4. A read cut short by the page cap (more than 10,000 tasks changed in the window) reports what it read and keeps
   every task it read in the map, so the next pass reports none of them again. The cursor then moves to the newest
   ``updated`` the read returned, or, when that is not past the cursor (the cap was filled by tasks inside the
   margin), one margin forward. Either way the next pass asks for a later window, so a cap that stays in place
   never stalls the list; tasks the capped read did not return and that fall behind the new window are lost, and
   the log shows ``capped=N``.
5. After all lists, one ``tasks-synced`` item says which lists changed and how many tasks.

State: ``tasks_poll_state`` ``{lists: {<list id>: {updated, seen: {<task id>: <updated>}}}}``. The runtime stages
every write and applies it after the journal has the result, so a retried tick reports the same items and advances
the cursor once. Lists the account no longer has, or that ``tasklists`` no longer names, are dropped from the
state. Google calls go through ``hookspool.providers.google`` on ``ctx.oauth``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hookspool.providers import google as G

OUTPUTS = ("task-changed", "tasks-synced")           # the manifest's [outputs]
STATE = "tasks_poll_state"
SOURCE = "google-tasks"
IDS_KEPT = G.TASK_PAGES * G.TASK_PAGE_SIZE            # tasks remembered inside the margin (at least one full read)
MARGIN = timedelta(seconds=60)                        # how far behind the cursor each pass looks again


def moment(value) -> datetime | None:
    """An RFC 3339 time from Tasks as an aware datetime, or None."""
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def stamp(at: datetime) -> str:
    """``at`` as Google writes times (UTC, milliseconds, ``Z``), which is also how ``updatedMin`` is compared."""
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}Z"


def followed(lists: list[dict], wanted) -> tuple[list[dict], int]:
    """(the lists named by the ``tasklists`` setting (ids exact, titles ignoring case; all of them when it is empty),
    how many of the names matched no list)."""
    names = [str(w).strip() for w in wanted or [] if str(w).strip()]
    if not names:
        return lists, 0
    ids = {str(x["id"]) for x in lists}
    titles = {str(x.get("title") or "").strip().lower() for x in lists}
    unmatched = sum(1 for n in names if n not in ids and n.lower() not in titles)
    return [x for x in lists if str(x["id"]) in names
            or str(x.get("title") or "").strip().lower() in {n.lower() for n in names}], unmatched


def newest(tasks: list[dict]) -> str:
    """The greatest ``updated`` among ``tasks`` as Google wrote it, or an empty string."""
    best, best_at = "", None
    for t in tasks:
        at = moment(t.get("updated"))
        if at is not None and (best_at is None or at > best_at):
            best, best_at = str(t["updated"]), at
    return best


def kept(seen: dict[str, str], cursor: datetime | None, read=()) -> dict[str, str]:
    """``seen`` cut to the tasks inside the margin of ``cursor``. The ids in ``read`` (what this pass read) that are
    inside the margin all stay, whatever their number; the others fill up to ``IDS_KEPT``, newest first. So the next
    read, which asks for the margin again, never meets a task this pass reported and then forgot."""
    rows = [(moment(u), i, u) for i, u in seen.items()]
    rows = [r for r in rows if r[0] is not None and (cursor is None or r[0] >= cursor - MARGIN)]
    rows.sort(key=lambda r: (r[0], r[1]))
    read = set(read)
    mine = [r for r in rows if r[1] in read]
    others = [r for r in rows if r[1] not in read]
    room = max(IDS_KEPT - len(mine), 0)
    others = others[len(others) - room:] if room else []
    return {i: u for _at, i, u in sorted(mine + others, key=lambda r: (r[0], r[1]))}


def previous(entry) -> tuple[str, dict[str, str]]:
    """(cursor, {id: updated}) of a stored list entry; the first-draft ``ids`` shape is read as ids at the cursor."""
    if not isinstance(entry, dict):
        return "", {}
    cursor = str(entry.get("updated") or "")
    seen = entry.get("seen")
    if isinstance(seen, dict):
        return cursor, {str(k): str(v) for k, v in seen.items()}
    return cursor, {str(i): cursor for i in entry.get("ids") or []}


def item_for(list_id: str, t: dict, titles: bool) -> dict:
    item = {"event_type": "task-changed", "source": SOURCE, "key": "task:" + str(t["id"]),
            "tasklist_id": list_id, "task_id": str(t["id"]), "status": t.get("status") or "needsAction",
            "updated": t.get("updated") or "", "due": t.get("due") or "", "deleted": bool(t.get("deleted"))}
    if titles:
        item["title"] = t.get("title") or ""
    return item


def poll(batch, ctx) -> list[dict]:
    s = ctx.settings
    api, titles = str(s["api_url"]), bool(s["include_titles"])
    client = ctx.oauth("cred")
    state = ctx.load_state(STATE, {})
    known = state.get("lists") if isinstance(state.get("lists"), dict) else {}

    chosen, unmatched = followed(G.tasks_lists(client, base=api), s["tasklists"])
    nxt: dict[str, dict] = {}
    changed: list[dict] = []
    touched: list[str] = []
    bootstrapped = capped = gone = 0

    for lst in chosen:
        lid = str(lst["id"])
        first = lid not in known or not isinstance(known.get(lid), dict)
        cursor, seen = previous(known.get(lid))
        cursor_at = moment(cursor)
        low = stamp(cursor_at - MARGIN) if cursor_at is not None else ""
        try:
            tasks, finished = G.tasks_changes(client, lid, low, base=api, titles=titles)
        except G.TasklistGone:
            gone += 1
            continue
        capped += 0 if finished else 1
        if first:                                         # first sight of this list: record where it stands
            bootstrapped += 1
            if finished:
                top = newest(tasks)
                nxt[lid] = {"updated": top, "seen": kept({str(t["id"]): str(t.get("updated") or "") for t in tasks},
                                                         moment(top))}
            else:                                         # too big to read whole: start from now, not from a partial read
                nxt[lid] = {"updated": stamp(datetime.fromtimestamp(ctx.now(), timezone.utc)), "seen": {}}
            continue
        fresh = []
        for t in tasks:
            at = moment(t.get("updated"))
            if at is None or (str(t["id"]) in seen and moment(seen[str(t["id"])]) == at):
                continue
            fresh.append((at, str(t["id"]), t))
        fresh.sort(key=lambda x: (x[0], x[1]))
        changed.extend(item_for(lid, t, titles) for _at, _tid, t in fresh)
        if fresh:
            touched.append(lid)
        merged = dict(seen)
        merged.update({str(t["id"]): str(t.get("updated")) for t in tasks if moment(t.get("updated")) is not None})
        top = newest(tasks)
        if top and (cursor_at is None or moment(top) > cursor_at):
            cursor, cursor_at = top, moment(top)
        elif not finished and cursor_at is not None:      # the cap was filled inside the margin: step one margin on
            cursor_at += MARGIN
            cursor = stamp(cursor_at)
        nxt[lid] = {"updated": cursor, "seen": kept(merged, cursor_at, read=(str(t["id"]) for t in tasks))}

    ctx.save_state(STATE, {"lists": nxt})
    out = list(changed)
    if changed:
        out.append({"event_type": "tasks-synced", "source": SOURCE, "key": "tasks", "tasklists": touched,
                    "changed_count": len(changed)})
    ctx.log(f"batch {batch.id}: lists={len(chosen)} bootstrapped={bootstrapped} changed={len(changed)}"
            + (f" capped={capped}" if capped else "") + (f" gone={gone}" if gone else "")
            + (f" unmatched={unmatched}" if unmatched else ""))
    return out
