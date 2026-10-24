"""Google Contacts poller: which contacts changed since the last poll, by resource name and nothing else.

The People API has no push, so the dispatcher's clock starts this poll every ``every_s`` seconds. One pass asks
``people.connections.list`` (and, with ``include_other_contacts``, ``otherContacts.list``) for what changed since
the stored sync token. The request asks for ``metadata`` only, so a name, address or number never reaches this
process, and the item carries resource names (``people/c123``) and counts. A deployment's own job reads details.

1. No stored token (the first pass, or the list was just switched on): read the whole list once, keep the sync
   token, report nothing.
2. A stored token: report the resource names that changed and those that were deleted, if there are any, and keep
   the new token.
3. Google refused the token (it expires seven days after a full read): read the whole list again, keep the new
   token and report ``full_resync``: ``changed`` is then every contact that exists now and ``deleted`` is empty.

An item is bounded. Resource names go out on the processor's stdout (capped at 1 MiB) and on to a destination with
a body limit of its own, so an item names at most ``MAX_NAMES`` contacts, ``changed`` and ``deleted`` together. A
pass that finds more (a weekly full read of a large book, or a bulk import) sends both lists empty, sets
``truncated`` true and keeps ``total_changed`` as the real count. The token still advances, so the consumer must
read the whole address book itself when it sees ``truncated``.

State: ``contacts_sync`` ``{"connections": {"token", "page_size"}, "other": {...}}``. The runtime applies it only
after its journal holds the result, so a retried batch reports the same item and advances each token once.
"""
from __future__ import annotations

from hookspool.providers import google as G

STATE = "contacts_sync"
EVENT = "contacts-changed"
LISTS = (("connections", False), ("other", True))
# Worst case an item is MAX_NAMES names of about 175 bytes: 350 KB, so two items (both lists) stay under the
# 1 MiB stdout cap of the processor and the 1 MiB body limit of a destination.
MAX_NAMES = 2000


def _read(client, cursor: dict, cfg: dict, other: bool) -> tuple[dict | None, dict | None]:
    """(the new cursor of one list, the item to report or None)."""
    token = str(cursor.get("token") or "")
    size = int(cursor.get("page_size") or cfg["page_size"]) if token else cfg["page_size"]
    resync = False
    try:
        changed, deleted, nxt = G.people_changes(client, token, other=other, base=cfg["api"], page_size=size)
    except G.SyncGone:
        changed, deleted, nxt = G.people_changes(client, "", other=other, base=cfg["api"], page_size=cfg["page_size"])
        size, resync = cfg["page_size"], True
    fresh = {"token": nxt, "page_size": size}
    if not token and not resync:
        return fresh, None                                   # the bootstrap: a token, no report
    if not (changed or deleted or resync):
        return fresh, None
    total = len(changed) + len(deleted)
    cut = total > MAX_NAMES
    return fresh, {"event_type": EVENT, "changed": [] if cut else changed, "deleted": [] if cut else deleted,
                   "full_resync": resync, "truncated": cut, "total_changed": total}


def poll(batch, ctx) -> list[dict]:
    size = ctx.settings["page_size"]
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= G.PEOPLE_MAX_PAGE_SIZE:
        ctx.fail_permanent("page_size must be between 1 and 1000")
    cfg = {"page_size": size, "api": str(ctx.settings["api_url"])}
    follow = {"connections": True, "other": bool(ctx.settings["include_other_contacts"])}
    client = ctx.oauth("cred")
    state = ctx.load_state(STATE, {})
    fresh: dict[str, dict] = {}
    items: list[dict] = []
    log: list[str] = []
    for name, other in LISTS:
        if not follow[name]:
            continue                                         # switched off: its token is dropped, a return starts over
        cursor = state.get(name) if isinstance(state.get(name), dict) else {}
        new, item = _read(client, cursor, cfg, other)
        fresh[name] = new
        if item is not None:
            items.append(item)
        log.append(f"{name} " + ("bootstrap" if not cursor.get("token") and item is None else
                                  "idle" if item is None else
                                  f"total={item['total_changed']}"
                                  + (" full_resync" if item["full_resync"] else "")
                                  + (" truncated" if item["truncated"] else "")))
    ctx.save_state(STATE, fresh)
    ctx.log("poll " + "; ".join(log))
    return items

