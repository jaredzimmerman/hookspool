"""Google Tasks poller: the Tasks API has no push, so every tick asks each followed list what changed.

For each followed task list one ``tasks.list`` call (``updatedMin`` = the newest ``updated`` seen in that list,
completed, hidden and deleted tasks included) returns the tasks that changed since the last pass. No model is
involved and no task text is read unless ``include_titles`` is on. A pass:

1. Lists the account's task lists and keeps those named by ``tasklists`` (by id, or by title ignoring case); an
   empty setting keeps all.
2. A list the state has not seen yet is only recorded: its newest ``updated`` and the ids at that instant are
   stored and nothing is reported, so the first pass (and a list added later) emits no history.
3. A list already recorded is read from its stored ``updated``. A task whose ``updated`` equals the stored one
   and whose id was already reported at that instant is skipped (the filter is inclusive, so this is how a task is
   never reported twice); every other returned task becomes a ``task-changed`` item.
4. After all lists, one ``tasks-synced`` item says which lists changed and how many tasks.

State: ``tasks_poll_state`` ``{lists: {<list id>: {updated, ids}}}``. The runtime stages every write and applies
it after the journal has the result, so a retried tick reports the same items and advances the cursor once. Lists
the account no longer has, or that ``tasklists`` no longer names, are dropped from the state. Google calls go
through ``hookspool.providers.google`` on ``ctx.oauth``.
"""
from __future__ import annotations

from datetime import datetime, timezone

from hookspool.providers import google as G

OUTPUTS = ("task-changed", "tasks-synced")           # the manifest's [outputs]
STATE = "tasks_poll_state"
SOURCE = "google-tasks"
IDS_KEPT = 5000                                       # ids remembered at the newest instant of one list


def moment(value) -> datetime | None:
    """An RFC 3339 time from Tasks as an aware datetime, or None."""
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def followed(lists: list[dict], wanted) -> list[dict]:
    """The lists named by the ``tasklists`` setting (ids exact, titles ignoring case); all of them when it is empty."""
    names = [str(w).strip() for w in wanted or [] if str(w).strip()]
    if not names:
        return lists
    low = {n.lower() for n in names}
    return [x for x in lists if str(x["id"]) in names or str(x.get("title") or "").strip().lower() in low]


def newest(tasks: list[dict]) -> tuple[str, list[str]]:
    """(the greatest ``updated`` among ``tasks`` as Google wrote it, the ids that carry it)."""
    best, best_at = "", None
    for t in tasks:
        at = moment(t.get("updated"))
        if at is not None and (best_at is None or at > best_at):
            best, best_at = str(t["updated"]), at
    ids = sorted(str(t["id"]) for t in tasks if best_at is not None and moment(t.get("updated")) == best_at)
    return best, ids


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

    chosen = followed(G.tasks_lists(client, base=api), s["tasklists"])
    nxt: dict[str, dict] = {}
    changed: list[dict] = []
    touched: list[str] = []
    bootstrapped = capped = gone = 0

    for lst in chosen:
        lid = str(lst["id"])
        prev = known.get(lid) if isinstance(known.get(lid), dict) else None
        hw = str(prev.get("updated") or "") if prev else ""
        try:
            tasks, finished = G.tasks_changes(client, lid, hw, base=api, titles=titles)
        except G.TasklistGone:
            gone += 1
            continue
        capped += 0 if finished else 1
        top, top_ids = newest(tasks)
        if prev is None:                                  # first sight of this list: record where it stands
            nxt[lid] = {"updated": top, "ids": top_ids[:IDS_KEPT]}
            bootstrapped += 1
            continue
        hw_at, seen = moment(hw), set(prev.get("ids") or [])
        fresh = []
        for t in tasks:
            at = moment(t.get("updated"))
            if hw_at is not None and (at is None or at < hw_at or (at == hw_at and str(t["id"]) in seen)):
                continue
            fresh.append((at, str(t["id"]), t))
        fresh.sort(key=lambda x: (x[0] or datetime.min.replace(tzinfo=timezone.utc), x[1]))
        changed.extend(item_for(lid, t, titles) for _at, _tid, t in fresh)
        if fresh:
            touched.append(lid)
        if top and (hw_at is None or moment(top) > hw_at):
            nxt[lid] = {"updated": top, "ids": top_ids[:IDS_KEPT]}
        elif top and moment(top) == hw_at:                # nothing newer: remember the ids seen at the same instant
            nxt[lid] = {"updated": hw, "ids": sorted(seen | set(top_ids))[:IDS_KEPT]}
        else:
            nxt[lid] = {"updated": hw, "ids": sorted(seen)}

    ctx.save_state(STATE, {"lists": nxt})
    out = list(changed)
    if changed:
        out.append({"event_type": "tasks-synced", "source": SOURCE, "key": "tasks", "tasklists": touched,
                    "changed_count": len(changed)})
    ctx.log(f"batch {batch.id}: lists={len(chosen)} bootstrapped={bootstrapped} changed={len(changed)}"
            + (f" capped={capped}" if capped else "") + (f" gone={gone}" if gone else ""))
    return out
