"""Gmail processor: a push for a mailbox -> is there new mail the agent should see?

Runs for one batch of Pub/Sub notifications (one mailbox per batch, but a batch may name several).
The notifications only say that a mailbox changed; the processor reads Gmail's history from the
history id it stored, so extra pushes cost nothing and lose nothing. No model is involved.

For each watched mailbox (the owner's, and optionally an assistant's own):

* the owner's mailbox: new inbox mail that asks for the owner's availability becomes ``scheduling``,
  carrying the owner's real free windows from Google free/busy;
* the assistant's mailbox: new inbox mail becomes ``agent-mail``;
* mail the deployment's hook claims becomes the item the hook returns (``agent-schedule`` in the
  reference deployment), see ``hooks.classify`` in README.md;
* every pass that read new mail also reports ``mailbox-changed`` (one per mailbox per batch), so a
  deployment can wake jobs that watch mail.

State, per mailbox, under ``gmail_push_state`` (owner) and the ``agent_state`` setting (assistant):
``{historyId, handled: [message ids], bootstrapped}``. The runtime stages the writes and applies them
only after the journal has the result, so a retried batch never loses an item and never advances a
history id twice. Google calls go through ``hookspool.providers.google`` on ``ctx.oauth``; the credential
of each mailbox is its own ``processor = true`` secret.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hookspool.providers import google as G

OUTPUTS = ("scheduling", "agent-mail", "agent-schedule", "mailbox-changed")   # the manifest's [outputs]
OWNER_STATE = "gmail_push_state"
HANDLED_KEEP = 300
MIN_WINDOW_MIN = 45
DAYS_AHEAD = 7
SCHEDULE_DAYS_AHEAD = 14
SKIP_LABELS = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS", "SPAM", "TRASH"}
SCHEDULING = re.compile(
    r"\b(availability|available|avail|what (times?|days?) (work|suit)|times? that work|"
    r"schedule (a |an |the |some )?(call|meeting|time|chat|interview|conversation)|"
    r"propose (a |some )?times?|time slots?|slots?|your calendar|book (a |some )?time|"
    r"set up (a |some )?time|find (a |some )?time|when (are|would) you)\b", re.I)
RECRUITING = re.compile(r"\b(interview|offer|hire|hiring|recruit\w*|opportunity|next steps?)\b")
NO_REPLY = re.compile(r"^(no-?reply|noreply|donotreply|notifications?|mailer-daemon)@")


# ---- settings ----------------------------------------------------------------------------------------------
def _hours(text: str) -> tuple[int, int, int, int]:
    try:
        a, b = text.split("-")
        (h1, m1), (h2, m2) = (tuple(int(x) for x in t.strip().split(":")) for t in (a, b))
        return h1, m1, h2, m2
    except ValueError:
        return 9, 0, 17, 30


def _names(values) -> list[str]:
    return [str(v).strip().lower().lstrip("@") for v in values if str(v).strip()]


class Config:
    """The instance's settings, read once and normalised."""

    def __init__(self, ctx):
        s = ctx.settings
        self.owner = str(s["owner"]).strip().lower()
        self.agent = str(s["agent"]).strip().lower()
        if not self.owner:
            ctx.fail_permanent("the owner setting is empty")
        try:
            self.tz_name = str(s["tz"])
            self.tz = ZoneInfo(self.tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            ctx.fail_permanent("the tz setting is not a time zone")
        self.hours = _hours(str(s["hours"]))
        self.own_domains = _names(s["own_domains"])
        self.skip_domains = _names(s["skip_domains"])
        self.skip_keywords = [str(k).strip() for k in s["skip_keywords"] if str(k).strip()]
        self.important = _names(s["important_senders"])
        self.api = str(s["api_url"])
        self.freebusy_url = str(s["freebusy_url"])
        agent_state = str(s["agent_state"]).strip()
        if agent_state.endswith(".json"):
            agent_state = agent_state[:-5]
        # mailbox -> (state name, credential secret)
        self.accounts = {self.owner: (OWNER_STATE, "cred_owner")}
        if self.agent:
            self.accounts[self.agent] = (agent_state or "gmail_push_state_agent", "cred_agent")


# ---- the filter -------------------------------------------------------------------------------------------------
def _domain_hit(frm: str, dom: str, entries: list[str]) -> str | None:
    for d in entries:
        if "@" in d:
            if frm == d:
                return d
        elif dom == d or dom.endswith("." + d):
            return d
    return None


def skip_reason(msg: dict, cfg: Config, account: str) -> str | None:
    """Why this message never goes to the agent, or None."""
    labels = set(msg["labels"])
    if labels & SKIP_LABELS or "INBOX" not in labels:
        return "not an inbox message" if "INBOX" not in labels else "promotional/social"
    frm = G.address(msg["headers"].get("from", ""))
    if frm == account or frm == cfg.owner or (cfg.agent and frm == cfg.agent) \
            or any(frm.endswith("@" + d) for d in cfg.own_domains):
        return "from us"
    dom = frm.split("@")[-1]
    if _domain_hit(frm, dom, cfg.important):
        return None                                    # an important sender is never skipped below
    hit = _domain_hit(frm, dom, cfg.skip_domains)
    if hit:
        return f"skip domain {hit}"
    if NO_REPLY.match(frm):
        return "no-reply sender"
    subj = msg["headers"].get("subject", "").lower()
    # Recruiting and job mail is never skipped on a keyword.
    if RECRUITING.search(subj):
        return None
    # Whole-word match: a substring test skipped every "Salesforce" subject on "sale".
    for k in cfg.skip_keywords:
        if re.search(r"(?<![a-z0-9])" + re.escape(k.lower()) + r"(?![a-z0-9])", subj):
            return f"skip keyword {k!r} in subject"
    return None


# ---- free windows ---------------------------------------------------------------------------------------------
def free_windows(client, cfg: Config, now: datetime, days: int = DAYS_AHEAD, cap: int = 12) -> list[str]:
    """The owner's free windows (at least ``MIN_WINDOW_MIN``) in working hours over the next ``days``
    weekdays, from Google free/busy on the owner's own calendar (not the household's)."""
    tz = cfg.tz
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    busy = [(a.astimezone(tz), b.astimezone(tz)) for a, b in
            G.freebusy(client, start, start + timedelta(days=days + 1), cfg.tz_name, url=cfg.freebusy_url)]
    h1, m1, h2, m2 = cfg.hours
    out = []
    for d in range(days + 1):
        day = start + timedelta(days=d)
        if day.weekday() >= 5:
            continue
        cur = day.replace(hour=h1, minute=m1)
        end = day.replace(hour=h2, minute=m2)
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


# ---- items ------------------------------------------------------------------------------------------------------
def _received(msg: dict, tz) -> str:
    return datetime.fromtimestamp(msg["internal_date"] / 1000, tz).strftime("%a %b %-d %-I:%M %p")


def _item_key(item: dict) -> str:
    return f"{item.get('account')}:{item.get('tracked_thread_id') or item.get('thread_id') or item.get('message_id')}"


def _standard_fields(item: dict, msg: dict, account: str) -> dict:
    """The fields every item of this bobbin carries; a hook's own values win."""
    item.setdefault("account", account)
    item.setdefault("source", "gmail")
    item.setdefault("message_id", msg["id"])
    item.setdefault("thread_id", msg["thread_id"])
    item.setdefault("rfc_message_id", msg["headers"].get("message-id", ""))
    item.setdefault("key", _item_key(item))
    return item


def _hook_item(ctx, msg: dict, account: str) -> dict | None:
    """``hooks.classify(message, account, ctx)``: None to fall through, else a complete item."""
    classify = getattr(ctx.hooks, "classify", None) if ctx.hooks is not None else None
    if not callable(classify):
        return None
    item = classify(dict(msg, headers=dict(msg["headers"]), labels=list(msg["labels"])), account, ctx)
    if item is None:
        return None
    if not isinstance(item, dict) or item.get("event_type") not in OUTPUTS:
        ctx.fail_permanent("hooks.classify returned an item whose event_type is not a declared output")
    return _standard_fields(dict(item), msg, account)


def _scheduling_item(msg: dict, cfg: Config, account: str, text: str) -> dict:
    frm = msg["headers"].get("from", "")
    return _standard_fields({
        "event_type": "scheduling" if account == cfg.owner else "agent-mail", "from": frm,
        "from_address": G.address(frm), "from_domain": G.address(frm).split("@")[-1],
        "subject": msg["headers"].get("subject", ""), "received": _received(msg, cfg.tz),
        "excerpt": text[:1500].strip()}, msg, account)


# ---- one mailbox ------------------------------------------------------------------------------------------------
def _pass(ctx, cfg: Config, account: str, now: datetime) -> list[dict]:
    state_name, secret = cfg.accounts[account]
    label = "owner" if account == cfg.owner else "agent"  # logs never carry the address
    client = ctx.oauth(secret)
    state = ctx.load_state(state_name, {})

    def bootstrap(reason: str) -> None:
        prof = G.gmail_profile(client, cfg.api)
        ctx.save_state(state_name, {"historyId": str(prof["historyId"]), "handled": state.get("handled") or [],
                                    "bootstrapped": now.isoformat()})
        ctx.log(f"bootstrap {label} ({reason}) historyId={prof['historyId']}")

    if not state.get("historyId"):
        bootstrap("no history id")
        return []
    try:
        ids, newest = G.gmail_history(client, state["historyId"], base=cfg.api)
    except G.HistoryGone:                              # the history id is too old: start over quietly
        bootstrap("history 404")
        return []

    handled = state.get("handled") or []
    hits: list[dict] = []
    looked = 0
    for mid in ids:
        if mid in handled:
            continue
        raw = G.gmail_message(client, mid, cfg.api)
        if raw is None:
            continue
        looked += 1
        msg = G.parse_message(raw)
        if "INBOX" in msg["labels"]:
            item = _hook_item(ctx, msg, account)
            if item is not None:                       # a private hand-off: never the generic path
                hits.append(item)
                continue
        h = msg["headers"]
        if account == cfg.owner and cfg.agent and cfg.agent in (h.get("to", "") + h.get("cc", "")).lower():
            continue                                   # the agent is on this thread: it handles it, no draft here
        if skip_reason(msg, cfg, account):
            continue
        text = msg["body"]
        if account == cfg.owner and not SCHEDULING.search(h.get("subject", "") + "\n" + text[:4000]):
            continue
        hits.append(_scheduling_item(msg, cfg, account, text))
    handled = (handled + [i for i in ids if i not in handled])[-HANDLED_KEEP:]
    ctx.save_state(state_name, {"historyId": str(newest), "handled": handled,
                                "bootstrapped": state.get("bootstrapped")})

    out: list[dict] = []
    if hits:
        sched = [x for x in hits if x["event_type"] == "agent-schedule"]
        owner_client = ctx.oauth(cfg.accounts[cfg.owner][1])
        if account == cfg.owner:
            # every request is reported: a coalesced batch can hold several, and one free/busy read serves all
            plain = [x for x in hits if x["event_type"] != "agent-schedule"]
            if plain:
                windows = free_windows(owner_client, cfg, now)
                text = "\n".join("- " + w for w in windows) or "- (no free working-hours window in the next week)"
                for x in plain:
                    if x["event_type"] == "scheduling":
                        x["free_windows_text"] = text
        if sched:
            windows = free_windows(owner_client, cfg, now, days=SCHEDULE_DAYS_AHEAD, cap=20)
            wt = "\n".join("- " + w for w in windows) or "- (no free working-hours window in the next two weeks)"
            for x in sched:
                x.setdefault("free_windows_text", wt)
        out = sched + [x for x in hits if x["event_type"] != "agent-schedule"]
        ctx.log(f"push {label} new={len(ids)} looked={looked} -> {len(out)} item(s)"
                + (f" ({len(sched)} schedule)" if sched else ""))
    else:
        ctx.log(f"push {label} new={len(ids)} looked={looked} -> silent")
    if looked:
        out.append({"event_type": "mailbox-changed", "source": "gmail", "account": account, "key": account,
                    "new_messages": looked, "history_id": str(newest)})
    return out


# ---- the batch --------------------------------------------------------------------------------------------------
def process(batch, ctx) -> list[dict]:
    cfg = Config(ctx)
    latest: dict[str, dict] = {}                       # one pass per mailbox: history is read from the stored id
    for p in batch.events:
        latest[str(p.get("emailAddress") or cfg.owner).lower()] = p
    now = datetime.fromtimestamp(ctx.now(), cfg.tz)
    items: list[dict] = []
    for account in latest:
        if account not in cfg.accounts:
            ctx.log("push for an unwatched mailbox; ignored")
            continue
        items += _pass(ctx, cfg, account, now)
    return items
