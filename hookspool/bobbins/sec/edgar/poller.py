"""sec.edgar poller: one conditional GET per (company, form) Atom feed, or one current-events feed per
form in current mode; emits the filings it has not seen.

EDGAR has no push. Its fair-access rules (a User-Agent with contact information, at most 10 requests a
second) are kept here: the setting is required and checked, and ``ctx.pace`` spaces the requests (core does
the waiting).
"""
import json
import re
from pathlib import Path

from hookspool import feeds, net
from hookspool import bobbin_loader as BL

FEED_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
REQUESTS_PER_S = 5            # half of EDGAR's limit, so a second instance or a person on the same address has room
MAX_FEEDS = 200               # one tick must fit its timeout: 200 requests at 5 a second is 40 s plus the transfers
KEEP_SEEN = 100               # accessions remembered per feed
MAX_BACKFILL = 40
PAGE = 40                     # entries in one company feed page (the count parameter)
CURRENT_PAGE = 100            # entries in one current-events feed page
CURRENT_MAX_PAGES = 10        # cap on pages fetched in current mode before a possible-gap line
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
TICKERS_REFRESH_S = 86400     # refresh company_tickers.json at most once a day

_CONTACT = re.compile(r"[^\s@<>()]+@[^\s@<>()]+\.[A-Za-z]{2,}")
_CIK = re.compile(r"^[0-9]{1,10}$")
_FORM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ./-]{0,23}$")
_ACCESSION = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")
_ACCESSION_IN_ID = re.compile(r"accession-number=([0-9]{10}-[0-9]{2}-[0-9]{6})")
_TITLE_CIK = re.compile(r"\s*\([0-9]{1,10}\)\s*$")
_TITLE_CIK_NUM = re.compile(r"\(([0-9]{1,10})\)\s*$")


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
    """(user_agent, ciks, forms, backfill, mode, tickers, tickers_file, all_filings), or the tick ends with a
    permanent failure that names the setting."""
    ua = ctx.settings["user_agent"]
    if not isinstance(ua, str) or not _CONTACT.search(ua) or len(ua) > 200 or any(c in ua for c in "\r\n"):
        ctx.fail_permanent("user_agent: EDGAR requires a User-Agent with contact information (a name and an email)")
    mode = ctx.settings.get("mode", "company")
    if not isinstance(mode, str) or mode not in ("company", "current"):
        ctx.fail_permanent("mode: must be company or current")
    raw_ciks = ctx.settings["ciks"]
    if not isinstance(raw_ciks, list):
        ctx.fail_permanent("ciks: a list of CIK numbers")
    ciks = [_cik(c) for c in raw_ciks]
    if None in ciks:
        ctx.fail_permanent("ciks: each entry is a CIK number of up to 10 digits")
    forms = ctx.settings["forms"]
    if not isinstance(forms, list) or not forms or not all(isinstance(f, str) and _FORM.match(f.strip()) for f in forms):
        ctx.fail_permanent("forms: list form types such as 8-K or 4")
    ciks, forms = _unique(ciks), _unique([f.strip() for f in forms])
    if mode == "company":
        if not ciks:
            ctx.fail_permanent("ciks: list at least one CIK number")
        if len(ciks) * len(forms) > MAX_FEEDS:
            ctx.fail_permanent(f"ciks x forms: at most {MAX_FEEDS} feeds in one instance")
    backfill = ctx.settings["backfill"]
    if isinstance(backfill, bool) or not isinstance(backfill, int) or not 0 <= backfill <= MAX_BACKFILL:
        ctx.fail_permanent(f"backfill: a number from 0 to {MAX_BACKFILL}")
    tickers = ctx.settings.get("tickers", [])
    if not isinstance(tickers, list) or not all(isinstance(t, str) and t.strip() for t in tickers):
        ctx.fail_permanent("tickers: a list of ticker symbols")
    tickers = [t.strip().upper() for t in tickers]
    tickers_file = ctx.settings.get("tickers_file", "")
    if tickers_file:
        if not isinstance(tickers_file, str) or not tickers_file:
            ctx.fail_permanent("tickers_file: an absolute path")
        problem = BL.code_path_problem(tickers_file, want="file")
        if problem:
            ctx.fail_permanent(f"tickers_file: {problem}")
    all_filings = ctx.settings.get("all_filings", False)
    if not isinstance(all_filings, bool):
        ctx.fail_permanent("all_filings: true or false")
    return ua.strip(), ciks, forms, backfill, mode, tickers, tickers_file, all_filings


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
    if cik is None:
        m = _TITLE_CIK_NUM.search(entry["title"] or "")
        cik = m.group(1).zfill(10) if m else ""
    if not company:
        company = _TITLE_CIK.sub("", entry["title"] or "").strip()
    return {"event_type": "filing", "key": cik, "cik": cik, "company": company,
            "form": fields.get("filing-type") or (cats[0]["term"] if cats else ""),
            "accession": acc, "filed": fields.get("filing-date") or entry["updated"][:10],
            "title": entry["title"], "link": link if link.startswith("https://") else ""}


def _read_tickers_file(path):
    text = Path(path).read_text(encoding="utf-8")
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(x).strip().upper() for x in data if str(x).strip()]
    except (ValueError, TypeError):
        pass
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line.upper())
    return out


def _company_tickers(ctx, ua, now, batch):
    """Return a ticker->cik dict. Refreshes at most once a day with a conditional GET."""
    state = ctx.load_state("tickers", {})
    if not isinstance(state, dict):
        state = {}
    cached = state.get("data")
    cached_at = state.get("cached_at", 0)
    if isinstance(cached, dict) and (now - cached_at) < TICKERS_REFRESH_S:
        return cached
    etag = state.get("etag", "")
    modified = state.get("modified", "")
    headers = {"User-Agent": ua}
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified
    pacer = ctx.pace(REQUESTS_PER_S)
    pacer.wait()
    try:
        status, text, resp = ctx.http.get_text(TICKERS_URL, headers=headers, label="tickers")
    except net.Permanent as e:
        if e.status == 403:
            ctx.log(f"batch {batch.id}: EDGAR answered HTTP 403; check user_agent and the request rate")
            raise net.Retryable("EDGAR refused the request (HTTP 403)", status=403) from None
        if e.status != 404:
            raise
        ctx.log(f"batch {batch.id}: company_tickers.json was not found (HTTP 404); skipped")
        if isinstance(cached, dict):
            state["cached_at"] = now
            ctx.save_state("tickers", state)
            return cached
        empty = {"data": {}, "cached_at": now, "etag": etag, "modified": modified}
        ctx.save_state("tickers", empty)
        return {}
    if status == 304:
        state = dict(state)
        state["cached_at"] = now
        ctx.save_state("tickers", state)
        return cached if isinstance(cached, dict) else {}
    try:
        raw = json.loads(text)
    except (ValueError, TypeError):
        ctx.log(f"batch {batch.id}: company_tickers.json could not be read; skipped")
        if isinstance(cached, dict):
            state = dict(state)
            state["cached_at"] = now
            ctx.save_state("tickers", state)
            return cached
        raise net.Retryable("company_tickers.json could not be read")
    data = {}
    if isinstance(raw, dict):
        for v in raw.values():
            if isinstance(v, dict):
                t = v.get("ticker")
                c = v.get("cik_str", v.get("cik"))
                if t is not None and c is not None:
                    try:
                        ci = str(int(c)).zfill(10)
                    except (ValueError, TypeError):
                        ci = str(c).strip().zfill(10)
                    data[str(t).strip().upper()] = ci
    new_state = {"data": data, "cached_at": now, "etag": resp.get("etag", etag or ""),
                 "modified": resp.get("last-modified", modified or "")}
    ctx.save_state("tickers", new_state)
    return data


def poll(batch, ctx) -> list[dict]:
    ua, ciks, forms, backfill, mode, tickers, tickers_file, all_filings = _config(ctx)
    state = ctx.load_state("feeds", {})
    pacer = ctx.pace(REQUESTS_PER_S)
    fetched = unchanged = skipped = 0
    new_state = {}
    out = []
    emitted = set()

    if mode == "company":
        wanted = {f"{c}|{f}" for c in ciks for f in forms}
        known = {}
        for key, fs in state.items():
            if "|" in key and not key.startswith("current|"):
                known.setdefault(key.split("|", 1)[0], set()).update(fs.get("seen") or [])
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
                    status, text, resp = ctx.http.get_text(FEED_URL, headers=headers, params=params, label="feed")
                except net.Permanent as e:
                    if e.status == 403:
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

    # ---- current mode ----
    all_tickers = list(tickers)
    if tickers_file:
        all_tickers.extend(_read_tickers_file(tickers_file))
    if all_tickers:
        ticker_map = _company_tickers(ctx, ua, ctx.now(), batch)
    else:
        ticker_map = {}
    watch_ciks = set(ciks)
    for t in all_tickers:
        c = ticker_map.get(t)
        if c:
            watch_ciks.add(c)
    cik_tickers = {}
    for t in all_tickers:
        c = ticker_map.get(t)
        if c:
            cik_tickers.setdefault(c, [])
            if t not in cik_tickers[c]:
                cik_tickers[c].append(t)
    if not watch_ciks and not all_filings:
        ctx.fail_permanent("current mode: a watchlist (tickers, tickers_file or ciks) is required unless "
                           "all_filings is true")

    wanted = {f"current|{f}" for f in forms}
    for form in forms:
        key = f"current|{form}"
        prev = state.get(key)
        prev_seen = set((prev or {}).get("seen") or [])
        first_resp = None
        page_items_all = []
        first_page_304 = False
        for page in range(CURRENT_MAX_PAGES):
            start = page * CURRENT_PAGE
            params = {"action": "getcurrent", "type": form, "company": "", "dateb": "", "owner": "include",
                      "start": start, "count": CURRENT_PAGE, "output": "atom"}
            headers = {"User-Agent": ua}
            if page == 0 and prev and prev.get("etag"):
                headers["If-None-Match"] = prev["etag"]
            if page == 0 and prev and prev.get("modified"):
                headers["If-Modified-Since"] = prev["modified"]
            pacer.wait()
            try:
                status, text, resp = ctx.http.get_text(FEED_URL, headers=headers, params=params, label="feed")
            except net.Permanent as e:
                if e.status == 403:
                    ctx.log(f"batch {batch.id}: EDGAR answered HTTP 403; check user_agent and the request rate")
                    raise net.Retryable("EDGAR refused the request (HTTP 403)", status=403) from None
                if e.status != 404:
                    raise
                skipped += 1
                ctx.log(f"batch {batch.id}: a feed was not found (HTTP 404); skipped")
                if prev:
                    new_state[key] = prev
                break
            if page == 0 and status == 304:
                first_page_304 = True
                unchanged += 1
                new_state[key] = prev
                break
            try:
                feed = feeds.parse_feed(text)
            except feeds.FeedError as e:
                skipped += 1
                ctx.log(f"batch {batch.id}: a feed could not be read ({e}); skipped")
                if prev:
                    new_state[key] = prev
                break
            fetched += 1
            if page == 0:
                first_resp = resp
            items = [i for i in (_filing(e, "", None) for e in feed["entries"]) if i]
            page_items_all.extend(items)
            page_accs = {i["accession"] for i in items}
            page_full = len(feed["entries"]) >= CURRENT_PAGE
            if page == 0 and page_full and prev and prev.get("seen") and not (page_accs & prev_seen):
                ctx.log(f"batch {batch.id}: possible gap: a feed returned a full page of new filings; "
                        "older ones may have been missed")
            if prev is None or (page_accs & prev_seen) or not page_full:
                break
            if page == CURRENT_MAX_PAGES - 1:
                ctx.log(f"batch {batch.id}: possible gap: current feed returned {CURRENT_MAX_PAGES} full pages; "
                        "older filings may have been missed")
                break

        if first_page_304:
            continue

        if not page_items_all:
            if prev:
                new_state[key] = prev
            continue

        seen_accs = []
        for i in page_items_all:
            if i["accession"] not in seen_accs:
                seen_accs.append(i["accession"])


        for i in reversed(page_items_all):
            acc_i = i["accession"]
            if acc_i in emitted:
                continue
            if watch_ciks and i["cik"] not in watch_ciks and not all_filings:
                continue
            if i["cik"] in cik_tickers:
                i["tickers"] = list(cik_tickers[i["cik"]])
            if acc_i not in prev_seen:
                emitted.add(acc_i)
                out.append(i)

        merged_seen = []
        for a in seen_accs + list((prev or {}).get("seen") or []):
            if a not in merged_seen:
                merged_seen.append(a)
        merged_seen = merged_seen[:KEEP_SEEN]
        etag = (first_resp or {}).get("etag", "") if first_resp else (prev or {}).get("etag", "")
        modified = (first_resp or {}).get("last-modified", "") if first_resp else (prev or {}).get("modified", "")
        accession = page_items_all[0]["accession"] if page_items_all else (prev or {}).get("accession", "")
        new_state[key] = {"etag": etag, "modified": modified, "accession": accession, "seen": merged_seen}

    if skipped and not fetched and not unchanged:
        raise net.Retryable("no EDGAR feed could be read this time")
    ctx.save_state("feeds", {k: v for k, v in new_state.items() if k in wanted})
    ctx.log(f"batch {batch.id}: {fetched} feed(s) read, {unchanged} unchanged, {skipped} skipped, "
            f"{len(out)} new filing(s)")
    return out
