---
bobbin: finnhub.earnings
provider: Finnhub Webhooks
webhook_docs: https://finnhub.io/docs/api/webhook
---

# Set up finnhub.earnings (for an agent)

The same procedure as SETUP.md, as steps. Finnhub's webhook page is thin: the header name, payload shape, destination
setting and retry behaviour are assumptions until the first real delivery confirms them (README, "What the provider
documents").

## Needs from the human

- An account at Finnhub that may create webhooks, and the list of tickers to watch.
- The webhook secret `secret`, written by the human to `/etc/hookspool/secrets/earnings_secret` (step 1). The agent
  never reads, prints or repeats its value.
- The Finnhub API key, written by the human to `/etc/hookspool/secrets/finnhub_api_key` (step 2). The agent never
  prints it.
- The public hooks host (`<HOOKS_HOST>`) that the proxy serves.
- Approval before any webhook is created at Finnhub (step 4, outward) and before any is deleted (step 6, outward).

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
3. **Goal:** the intake loads the bobbin with the configured secret, and the proxy forwards `POST /hooks/earnings`
   unchanged, including the body and the `X-Finnhub-Secret` header. **Check:** two commands. First the local replay,
   which sends nothing and never reads the secret value into the output:
   ```sh
   hookspool source test earnings --request hookspool/bobbins/finnhub/earnings/captures/bad_signature.http
   ```
   It must print `"result": "unverified"` and exit 1; `secret_unavailable` or exit 2 means the config or the secret
   file is wrong. Then one wrong-secret delivery through the public URL, which must be refused with 401:
   ```sh
   curl -sS -w '\n%{http_code}\n' -X POST -H 'Content-Type: application/json' -H 'X-Finnhub-Secret: wrong-test-secret' \
     --data '{"data":[{"symbol":"TEST","period":"2026-09-30","actual":9.99,"estimate":1.1}],"type":"earnings","note":"made-up test payload PAYLOAD-SENTINEL-7f3a"}' \
     https://<HOOKS_HOST>/hooks/earnings
   ```
   Do not send a delivery with the real secret: the agent does not handle its value, and a made-up `TEST` event would
   be queued to the real output. The right-secret path is checked by the first real delivery in step 5. **If it
   fails:** fix the proxy (it must not strip or rewrite headers on this path); a 2xx answer to the wrong secret means
   the request is not reaching the intake as sent. Do not go on to step 4 until both checks pass, because the bobbin
   does not assume the sender retries.
4. **Outward, ask first:** set the destination URL and the secret on the Finnhub account. **Unconfirmed:** the pages
   checked do not say where this is set, and the `webhook/add` call below carries only `event` and `symbol`. Find the
   setting in the account (the Webhooks section of the dashboard is where third-party guides point). If there is no
   such setting, stop and report it to the human. Then create one webhook per ticker `<SYMBOL>`:
   ```sh
   printf 'header = "X-Finnhub-Token: %s"\n' "$(cat /etc/hookspool/secrets/finnhub_api_key)" |
     curl -sS -K - -H 'Content-Type: application/json' -X POST -w '\n%{http_code}\n' \
       https://finnhub.io/api/v1/webhook/add --data '{"event":"earnings","symbol":"<SYMBOL>"}'
   ```
   Keep the id from each response. **Check:** run the list call, which must show one earnings webhook per ticker:
   ```sh
   printf 'header = "X-Finnhub-Token: %s"\n' "$(cat /etc/hookspool/secrets/finnhub_api_key)" |
     curl -sS -K - -w '\n%{http_code}\n' https://finnhub.io/api/v1/webhook/list
   ```
   **If it fails:** report the HTTP status (printed by the curl line) and the body without the key; do not retry more than once.
5. **Goal:** a real announcement is accepted. Wait for one (Finnhub sends it when the announcement is published), then
   run `hookspool list --source earnings`. **Check:** a queued batch names a ticker. **If it fails:** run
   `hookspool status`. If the batch is quarantined with a missing key, the payload is not shaped as `data.0.symbol`;
   report the payload's top-level keys (never its values) so the key path can be changed.
6. **Outward, rollback:** remove `[bobbins.earnings]`, run `hookspool check` and `hookspool reload`. Then ask the human
   to delete each webhook at Finnhub, by the ids kept in step 4. The deletion endpoint is not confirmed; if the API call
   fails, the human removes them in the dashboard. The agent does not do this without approval.
