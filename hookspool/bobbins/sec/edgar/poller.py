"""sec.edgar poller: one conditional GET per (company, form) Atom feed; emits the filings it has not seen.

EDGAR has no push. Its fair-access rules (a User-Agent with contact information, at most 10 requests a second) are
kept here: the setting is required and checked, and ``ctx.pace`` spaces the requests (core does the waiting).
"""
import re

from hookspool import feeds, net

FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
REQUESTS_PER_S = 5            # half of EDGAR's limit, so a second instance or a person on the same address has room
MAX_FEEDS = 200               # one tick must fit its timeout: 200 requests at 5 a second is 40 s plus the transfers
KEEP_SEEN = 100               # accessions remembered per feed
MAX_BACKFILL = 40
PAGE = 40                     # entries in one feed page (the count parameter); more new filings than this can be missed

_CONTACT = re.compile(r"[^\s@<>()]+@[^\s@<>()]+\.[A-Za-z]{2,}")
_CIK = re.compile(r"^[0-9]{1,10}$")
_FORM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ./-]{0,23}$")
_ACCESSION = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_ACCESSION_IN_ID = re.compile(r"accession-number=([0-9]{10}-[0-9]{2}-[0-9]{6})")
_TITLE_CIK = re.compile(r"\s*\([0-9]{1,10}\)\s*$")


def _cik(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    s = str(value).strip()
    return s.zfill(10) if _CIK.match(s) else None


def _unique(values):
    out = []
    for v in values:
        if v not in out:
            out.append(v)
    return out


def _config(ctx):
    """(user_agent, ciks, forms, backfill), or the tick ends with a permanent failure that names the setting."""
    ua = ctx.settings["user_agent"]
    if not isinstance(ua, str) or not _CONTACT.search(ua) or len(ua) > 200 or any(c in ua for c in "\r\n"):
        ctx.fail_permanent("user_agent: EDGAR requires a User-Agent with contact information (a name and an email)")
    raw_ciks = ctx.settings["ciks"]
    if not isinstance(raw_ciks, list) or not raw_ciks:
        ctx.fail_permanent("ciks: list at least one CIK number")
    ciks = [_cik(c) for c in raw_ciks]
    if None in ciks:
        ctx.fail_permanent("ciks: each entry is a CIK number of up to 10 digits")
    forms = ctx.settings["forms"]
    if not isinstance(forms, list) or not forms or not all(isinstance(f, str) and _FORM.match(f.strip()) for f in forms):
        ctx.fail_permanent("forms: list form types such as 8-K or 4")
    ciks, forms = _unique(ciks), _unique([f.strip() for f in forms])
    if len(ciks) * len(forms) > MAX_FEEDS:
        ctx.fail_permanent(f"ciks x forms: at most {MAX_FEEDS} feeds in one instance")
    backfill = ctx.settings["backfill"]
    if isinstance(backfill, bool) or not isinstance(backfill, int) or not 0 <= backfill <= MAX_BACKFILL:
        ctx.fail_permanent(f"backfill: a number from 0 to {MAX_BACKFILL}")
    return ua.strip(), ciks, forms, backfill


def _filing(entry, company, cik):
    """One feed entry as a filing item, or None when it has no valid accession number."""
    fields = entry["fields"]
    acc = fields.get("accession-number", "").strip()
    if not _ACCESSION.match(acc):
        m = _ACCESSION_IN_ID.search(entry["id"])
        acc = m.group(1) if m else ""
    if not acc:
        return None
    cats = entry["categories"]
    link = entry["link"] or fields.get("filing-href", "")
    return {"event_type": "filing", "key": cik, "cik": cik, "company": company,
            "form": fields.get("filing-type") or (cats[0]["term"] if cats else ""),
            "accession": acc, "filed": fields.get("filing-date") or entry["updated"][:10],
            "title": entry["title"], "link": link if link.startswith("https://") else ""}


def poll(batch, ctx) -> list[dict]:
    ua, ciks, forms, backfill = _config(ctx)
    state = ctx.load_state("feeds", {})
    pacer = ctx.pace(REQUESTS_PER_S)
    wanted = {f"{c}|{f}" for c in ciks for f in forms}
    known = {}                                       # per company: accessions any of its feeds has seen
    for key, fs in state.items():
        known.setdefault(key.split("|", 1)[0], set()).update(fs.get("seen") or [])
    new_state, out, emitted = {}, [], set()
    fetched = unchanged = skipped = 0
    for cik in ciks:
        for form in forms:
            key = f"{cik}|{form}"
            prev = state.get(key)
            headers = {"User-Agent": ua}
            if prev and prev.get("etag"):
                headers["If-None-Match"] = prev["etag"]
            if prev and prev.get("modified"):
                headers["If-Modified-Since"] = prev["modified"]
            params = {"action": "getcompany", "CIK": cik, "type": form, "dateb": "", "owner": "include",
                      "count": PAGE, "output": "atom"}
            pacer.wait()
            try:
                status, text, resp = ctx.http.get_text(FEED_URL, headers, params=params, label="feed")
            except net.Permanent as e:
                if e.status == 403:
                    # Stop at the first refusal: more requests while EDGAR blocks this address only make it worse.
                    ctx.log(f"batch {batch.id}: EDGAR answered HTTP 403; check user_agent and the request rate")
                    raise net.Retryable("EDGAR refused the request (HTTP 403)", status=403) from None
                if e.status != 404:
                    raise
                skipped += 1
                ctx.log(f"batch {batch.id}: a feed was not found (HTTP 404); skipped")
                if prev:
                    new_state[key] = prev
                continue
            if status == 304:
                unchanged += 1
                if prev:
                    new_state[key] = prev
                continue
            try:
                feed = feeds.parse_feed(text)
            except feeds.FeedError as e:
                skipped += 1
                ctx.log(f"batch {batch.id}: a feed could not be read ({e}); skipped")
                if prev:
                    new_state[key] = prev
                continue
            fetched += 1
            company = _TITLE_CIK.sub("", feed["title"])
            items = [i for i in (_filing(e, company, cik) for e in feed["entries"]) if i]     # newest first
            seen = [i["accession"] for i in items]
            if prev is None:
                fresh = [i for i in items[:backfill] if i["accession"] not in known.get(cik, ())]
            else:
                fresh = [i for i in items if i["accession"] not in known.get(cik, ())]
                if len(feed["entries"]) >= PAGE and prev.get("seen") and not set(seen) & set(prev["seen"]):
                    ctx.log(f"batch {batch.id}: possible gap: a feed returned a full page of new filings; "
                            "older ones may have been missed")
            for i in reversed(fresh):                                                             # oldest first
                if i["accession"] not in emitted:
                    emitted.add(i["accession"])
                    out.append(i)
            known.setdefault(cik, set()).update(seen)
            keep = seen + [a for a in (prev or {}).get("seen", []) if a not in seen]
            new_state[key] = {"etag": resp.get("etag", ""), "modified": resp.get("last-modified", ""),
                              "accession": seen[0] if seen else (prev or {}).get("accession", ""),
                              "seen": keep[:KEEP_SEEN]}
    if skipped and not fetched and not unchanged:
        raise net.Retryable("no EDGAR feed could be read this time")
    ctx.save_state("feeds", {k: v for k, v in new_state.items() if k in wanted})
    ctx.log(f"batch {batch.id}: {fetched} feed(s) read, {unchanged} unchanged, {skipped} skipped, "
            f"{len(out)} new filing(s)")
    return out
