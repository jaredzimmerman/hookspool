"""Google Drive processor: a push for the change feed -> one report item per changed file.

Runs for one batch of watch-channel pushes; a push only says that the feed moved, so one pass reads every change
since the stored page token (``changes.list``) whatever the batch holds (a ``sync`` ping only bootstraps).
No model is involved and no file content is ever read: an item carries identity, not contents. A pass:

1. Reads the feed from the stored page token, pages included. The first push, a ``sync`` ping, or a token Drive
   rejects (HTTP 404 or 410) only stores a fresh start page token and reports nothing.
2. Keeps the last change per file, and drops: removals (nothing left to describe), files in the trash (unless
   ``include_trashed``), MIME types in ``skip_mime_types``, and changes whose actor is one of the ``own``
   accounts (the account's own edits).
3. Names each remaining change. ``file-shared``: the file reached the account after the previous pass (its
   ``sharedWithMeTime`` is newer than the newest change that pass saw); ``file-created``: its ``createdTime`` is;
   otherwise ``file-changed``.

State: ``drive_push_state`` ``{pageToken, high_water, bootstrapped}``. ``high_water`` is the newest change time
seen, in Drive's own clock. The runtime stages every write and applies it after the journal has the result, so a
retried batch never loses a report and never advances the token twice. Drive calls go through
``hookspool.providers.google`` on ``ctx.oauth``.
"""
from __future__ import annotations

from datetime import datetime, timezone

from hookspool import net
from hookspool.providers import google as G

OUTPUTS = ("file-changed", "file-created", "file-shared")           # the manifest's [outputs]
STATE = "drive_push_state"
SOURCE = "google-drive"


class Config:
    def __init__(self, ctx):
        s = ctx.settings
        self.own = {str(a).strip().lower() for a in s["own"] if str(a).strip()}
        self.skip_exact = {str(m).strip().lower() for m in s["skip_mime_types"] if str(m).strip() and not str(m).endswith("/*")}
        self.skip_family = tuple(str(m).strip().lower()[:-1] for m in s["skip_mime_types"] if str(m).strip().endswith("/*"))
        self.include_trashed = bool(s["include_trashed"])
        self.drive_id = str(s["drive_id"]).strip()
        self.api = str(s["api_url"])

    def skips_mime(self, mime: str) -> bool:
        m = (mime or "").lower()
        return m in self.skip_exact or (bool(self.skip_family) and m.startswith(self.skip_family))


def moment(value) -> datetime | None:
    """An RFC 3339 time from Drive as an aware datetime, or None."""
    if not value:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def email_of(user) -> str:
    return str((user or {}).get("emailAddress") or "").strip().lower()


def latest_per_file(changes: list[dict]) -> list[dict]:
    """The last entry of each file, in the order those last entries appear."""
    last: dict[str, dict] = {}
    for c in changes:
        fid = c.get("fileId") or (c.get("file") or {}).get("id")
        if not fid:
            continue
        last.pop(fid, None)
        last[fid] = c
    return list(last.values())


def classify(f: dict, high_water: datetime | None) -> str:
    """``file-shared``, ``file-created`` or ``file-changed`` for a file seen in a pass that follows one whose newest
    change was at ``high_water``."""
    if high_water is not None:
        shared, created = moment(f.get("sharedWithMeTime")), moment(f.get("createdTime"))
        if shared and shared > high_water:
            return "file-shared"
        if created and created > high_water:
            return "file-created"
    return "file-changed"


def item_for(kind: str, change: dict, f: dict, actor: str) -> dict:
    return {"event_type": kind, "source": SOURCE, "key": "file:" + str(f.get("id") or change.get("fileId")),
            "file_id": f.get("id") or change.get("fileId"), "name": f.get("name") or "",
            "mime_type": f.get("mimeType") or "", "modified_time": f.get("modifiedTime") or "",
            "actor": actor, "web_link": f.get("webViewLink") or "", "change_time": change.get("time") or ""}


def _pass(ctx, cfg: Config, state_ev: str) -> list[dict]:
    client = ctx.oauth("cred")
    state = ctx.load_state(STATE, {})
    now = datetime.fromtimestamp(ctx.now(), timezone.utc)

    def bootstrap(reason: str) -> None:
        token = G.drive_start_page_token(client, base=cfg.api, drive_id=cfg.drive_id)
        if not token:
            raise net.Retryable("drive bootstrap returned no page token")
        ctx.save_state(STATE, {"pageToken": token, "high_water": now.isoformat(), "bootstrapped": now.isoformat()})
        ctx.log(f"bootstrap ({reason}) state={state_ev}")

    if state_ev == "sync" or not state.get("pageToken"):
        bootstrap("sync message" if state_ev == "sync" else "no page token")
        return []
    try:
        changes, token, finished = G.drive_changes(client, str(state["pageToken"]), base=cfg.api, drive_id=cfg.drive_id)
    except G.PageTokenGone:
        bootstrap("page token rejected")
        return []

    prev_hw = moment(state.get("high_water"))
    times = [t for t in (moment(c.get("time")) for c in changes) if t is not None]
    new_hw = max([prev_hw, *times]) if prev_hw else (max(times) if times else None)
    ctx.save_state(STATE, {"pageToken": token, "high_water": new_hw.isoformat() if new_hw else None,
                           "bootstrapped": state.get("bootstrapped")})

    out: list[dict] = []
    dropped = {"removed": 0, "trashed": 0, "mime": 0, "own": 0}
    for c in latest_per_file(changes):
        f = c.get("file") or {}
        if c.get("removed") or not f:
            dropped["removed"] += 1
            continue
        if f.get("trashed") and not cfg.include_trashed:
            dropped["trashed"] += 1
            continue
        if cfg.skips_mime(f.get("mimeType")):
            dropped["mime"] += 1
            continue
        kind = classify(f, prev_hw)
        actor = email_of(f.get("sharingUser")) if kind == "file-shared" else ""
        actor = actor or email_of(f.get("lastModifyingUser"))
        if actor and actor in cfg.own:
            dropped["own"] += 1
            continue
        out.append(item_for(kind, c, f, actor))
    ctx.log(f"ping state={state_ev} changes={len(changes)} -> {len(out)} item(s); dropped "
            + ", ".join(f"{k}={v}" for k, v in dropped.items()) + ("" if finished else "; more pending"))
    return out


def process(batch, ctx) -> list[dict]:
    cfg = Config(ctx)
    pushes = batch.events
    real = [p for p in pushes if str(p.get("resource_state") or p.get("event_type")) != "sync"]
    chosen = (real or pushes or [{}])[-1]
    return _pass(ctx, cfg, str(chosen.get("resource_state") or chosen.get("event_type") or ""))
