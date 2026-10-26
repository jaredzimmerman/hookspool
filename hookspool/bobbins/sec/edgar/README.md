---
bobbin: sec.edgar
provider: SEC EDGAR
api_docs: https://www.sec.gov/about/rss-feeds
---

# sec.edgar

New SEC filings for the companies you follow. EDGAR publishes RSS and Atom feeds and has no webhook, push or watch
method, so the dispatcher's clock ticks every 5 minutes and the poller asks each company's feed what is new. It emits one
`filing` item for each filing it has not seen.

## How it polls

- One Atom feed per company and form type:
  `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=<cik>&type=<form>&dateb=&owner=include&count=40&output=atom`.
- Each request is a conditional GET. The poller sends `If-None-Match` or `If-Modified-Since` when the last answer carried
  an `ETag` or `Last-Modified`, and a 304 means nothing changed. EDGAR's feed pages do not always send either header; the
  poller then reads the feed every time and picks out new filings by accession number, so the output is the same.
- Each feed lists only its newest 40 entries. If more than 40 new filings arrive between two successful reads (a busy
  Form 4 filer after an outage), the older ones are not seen. When a feed with saved state returns a full page and none of
  its filings was seen before, the poller logs `possible gap: a feed returned a full page of new filings`.
- State is kept per feed: the validators, the newest accession number and the last 100 accession numbers seen.
- The first time a feed is seen it is bootstrapped silently: nothing is emitted unless `backfill` asks for the newest few.
  Adding a company later bootstraps that company only.
- In current mode the poller reads one global current-events feed per form (`action=getcurrent`) and filters it by a
  watchlist of CIKs (from `ciks`, `tickers` and `tickers_file`). It pages through the feed until it reaches a filing
  it has seen before, up to 10 pages, and logs `possible gap` when it runs out of room. Ticker symbols are resolved to
  CIKs through SEC `company_tickers.json`, refreshed at most once a day.

## EDGAR's fair-access rules

EDGAR requires every automated client to identify itself and to stay under 10 requests a second
([rules](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data)).

- `user_agent` is required and has no default. The poller refuses to run, with a permanent failure, when it is empty or
  holds no email address. Use the form `Example Research research@example.com`, with a contact you answer.
- Requests go out at 5 a second, half the limit, spaced by core's `ctx.pace`. That leaves room for a second instance or a
  person on the same address. One instance covers up to 200 feeds (companies times forms).
- An HTTP 403 from EDGAR usually means the address broke a rule and is blocked for a while. The poller stops at the first
  refusal, logs `EDGAR answered HTTP 403; check user_agent and the request rate`, and the batch is retried on the
  dispatcher's normal retry schedule. The poller adds no delay of its own.

## Settings

| setting | type | default | meaning |
|---|---|---|---|
| `user_agent` | string, required | none | the User-Agent EDGAR requires: a name and an email address |
| `mode` | string | `"company"` | how to poll: `company` (one Atom feed per followed CIK, the default) or `current` (one global current-events feed per form, filtered by a watchlist) |
| `ciks` | list | `[]` | CIK numbers, with or without leading zeros. Required in company mode; in current mode they are part of the watchlist |
| `forms` | list | `["8-K", "4"]` | form types; EDGAR matches by prefix, so `4` also brings `4/A` |
| `backfill` | int | `0` | how many of the newest filings in a feed to emit the first time it is seen (0 to 40); filings another of the company's feeds already sent are not sent again |
| `tickers` | list | `[]` | ticker symbols for the watchlist in current mode; resolved to CIKs through SEC `company_tickers.json` |
| `tickers_file` | path | `""` | an absolute path to a watchlist file (one ticker per line, or a JSON list), re-read on every pass; same ownership and mode rules as a hooks file |
| `all_filings` | bool | `false` | in current mode, emit every filing when the watchlist is empty (documented opt-in) |

The poll interval is the manifest's `every_s = 300`. An instance may override it (60 or more). Polling only in market hours
is a deployment choice, not a setting here.

## Secrets

None. EDGAR's feeds are public.

## Outputs

`filing`: a new filing.

```json
{"event_type": "filing", "key": "0000000001", "cik": "0000000001", "company": "Example Corp", "form": "8-K",
 "accession": "0000000001-26-000002", "filed": "2026-01-06", "title": "8-K  - Current report",
 "link": "https://www.sec.gov/Archives/edgar/data/1/000000000126000002/0000000001-26-000002-index.htm"}
```

The `key` is the CIK, so filings of one company coalesce. Filings come out oldest first within a tick. A filing that
two of your feeds both match (`4` and `4/A`) is emitted once. In current mode an item may carry a `tickers` list with
the ticker symbols resolved for that company.

## Failures

- A feed that answers 404 or cannot be parsed is skipped for that tick and counted in the log. The others still run.
  When no feed can be read at all, the tick is retried.
- The feed parser refuses a document that has a DOCTYPE or an entity declaration, and caps its size, depth and entry count.

## Captures

- `captures/poll-new-filings.json`: a feed with two new 8-K filings (the kit instance sets `backfill = 5`).
- `captures/poll-not-modified.json`: EDGAR answers 304.

Both are made-up feeds for a made-up company.
