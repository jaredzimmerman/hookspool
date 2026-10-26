---
bobbin: sec.edgar
provider: SEC EDGAR
api_docs: https://www.sec.gov/about/rss-feeds
---

# Set up sec.edgar

This follows filings by company. EDGAR has no push, so the dispatcher polls its Atom feeds. [README.md](README.md) explains
the polling and EDGAR's fair-access rules.

Pick the instance name `edgar`.

## What you need

- A contact for EDGAR's User-Agent: a name and an email address that someone reads. EDGAR can block an address that
  does not identify itself.
- The CIK number of each company. Find one at [EDGAR company search](https://www.sec.gov/search-filings), where it is
  shown beside the company's name.

You register nothing at EDGAR and create no secret.

## Config

Add to `config.toml`, then run `hookspool check` and `hookspool reload`:

```toml
[bobbins.edgar]
bobbin = "sec.edgar"
settings = { user_agent = "Example Research research@example.com", ciks = ["0000000001"], forms = ["8-K", "4"] }
outputs = { filing = "hermes:edgar" }
```

To receive the newest few filings the first time instead of starting from now, add `backfill = 5` to `settings`.

## Current mode (large watchlists)

For a watchlist of hundreds of tickers, set `mode = "current"` and give `tickers` (and/or `tickers_file`) instead of
`ciks`:

```toml
[bobbins.edgar]
bobbin = "sec.edgar"
settings = { user_agent = "Example Research research@example.com", mode = "current", ciks = [], tickers = ["AAPL", "MSFT"], tickers_file = "/abs/path/tickers.txt", forms = ["8-K", "4"] }
outputs = { filing = "hermes:edgar" }
```

`tickers_file` is one ticker per line or a JSON list, re-read on every pass. An empty watchlist refuses to run unless
`all_filings = true`. See README.md for details.

## Verify

1. `hookspool status` and read `polls.edgar`: `next_at` is set.
2. After the first tick, `hookspool list --source edgar` shows a delivered batch. It carries no filings on the first pass
   unless `backfill` is set; the next new filing arrives as a `filing` item.
3. If `hookspool issues` shows `poll:edgar:stale`, read the dispatcher log: a permanent failure about `user_agent` means
   the setting lacks an email address, and the line `EDGAR answered HTTP 403` means EDGAR refused the request.

## Roll back

Remove the `[bobbins.edgar]` table, then run `hookspool check` and `hookspool reload`. Nothing exists at EDGAR to undo.
