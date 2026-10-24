---
bobbin: finnhub.earnings
provider: Finnhub Webhooks
webhook_docs: https://finnhub.io/docs/api/webhook
---

# Set up finnhub.earnings (for an agent)

The same procedure as SETUP.md, as steps. Finnhub's webhook page is thin: the header name, payload shape and retry
behaviour are assumptions until the first real delivery confirms them (README, "What the provider documents").

## Needs from the human

- An account at Finnhub that may create webhooks, and the list of tickers to watch.
- The webhook secret `secret`, written by the human to `/etc/hookspool/secrets/earnings_secret` (step 1). The agent
  never reads, prints or repeats its value.
- The Finnhub API key, written by the human to `/etc/hookspool/secrets/finnhub_api_key` (step 2). The agent never
  prints it.
- The public hooks host (`<HOOKS_HOST>`) that the proxy serves.
- Approval before any webhook is created at Finnhub (step 3, outward) and before any is deleted (rollback, outward).

Secret used below: `secret`. There are no settings.

## Steps

1. **Goal:** the secret file exists. The human writes the webhook secret to `/etc/hookspool/secrets/earnings_secret`
   with mode 0600. **Check:** `test -s /etc/hookspool/secrets/earnings_secret && ls -l /etc/hookspool/secrets/earnings_secret`
   shows mode `-rw-------`. **If it fails:** ask the human to write the file again.
2. **Goal:** the config table is in place. Add to `config.toml`:
   ```toml
   [bobbins.earnings]
   bobbin = "finnhub.earnings"
   secrets.secret = { file = "/etc/hookspool/secrets/earnings_secret" }
   outputs = { "*" = "hermes:<ROUTE>" }
   ```
   Then run `hookspool check` and `hookspool reload`. **Check:** both exit 0 and `hookspool bobbin expand earnings`
   shows the secret as a reference only. **If it fails:** read the error; it names the missing secret or output. Fix
   the table, not the bobbin.
3. **Outward, ask first:** create one webhook per ticker at Finnhub. Goal: Finnhub sends announcements for each ticker
   to `https://<HOOKS_HOST>/hooks/earnings`. For each ticker `<SYMBOL>`:
   ```sh
   printf 'header = "X-Finnhub-Token: %s"\n' "$(cat /etc/hookspool/secrets/finnhub_api_key)" |
     curl -sS -K - -H 'Content-Type: application/json' -X POST https://finnhub.io/api/v1/webhook/add \
       --data '{"event":"earnings","symbol":"<SYMBOL>"}'
   ```
   Keep the id from each response. **Check:** the list call (`GET https://finnhub.io/api/v1/webhook/list`, same header
   config) shows one earnings webhook per ticker. **If it fails:** report the HTTP status and the body without the key;
   do not retry more than once.
4. **Goal:** the reverse proxy forwards `POST /hooks/earnings` unchanged, including the body and the `X-Finnhub-Secret`
   header. **Check:** a captured delivery sent through the public URL with the wrong secret is refused, and one with the
   right secret is queued. **If it fails:** fix the proxy (it must not strip or rewrite headers on this path).
5. **Goal:** a real announcement is accepted. Wait for one (Finnhub sends it when the announcement is published), then
   run `hookspool list --source earnings`. **Check:** a queued batch names a ticker. **If it fails:** run
   `hookspool status`. If the batch is quarantined with a missing key, the payload is not shaped as `data.0.symbol`;
   report the payload's top-level keys (never its values) so the key path can be changed.
6. **Outward, rollback:** remove `[bobbins.earnings]`, run `hookspool check` and `hookspool reload`. Then ask the human
   to delete each webhook at Finnhub, by the ids kept in step 3. The deletion endpoint is not confirmed; if the API call
   fails, the human removes them in the dashboard. The agent does not do this without approval.
