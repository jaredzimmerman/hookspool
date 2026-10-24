---
bobbin: finnhub.earnings
provider: Finnhub Webhooks
webhook_docs: https://finnhub.io/docs/api/webhook
---

# Set up finnhub.earnings

This bobbin receives Finnhub's earnings webhook. Finnhub's documentation for it is thin (see the README), so the
steps below are the ones that worked for other integrations and must be checked on the first real delivery.

## What to create at Finnhub

1. Sign in to your Finnhub account and find your API key and the webhook secret the account shows for webhooks.
   The official page does not say where the secret is shown; the Webhooks section of the dashboard is where
   third-party guides point.
2. Create one earnings webhook per ticker, with the instance URL (`https://<your-hooks-host>/hooks/earnings`). The
   API calls are in the next section. Finnhub's REST reference is at <https://finnhub.io/docs/api>.

## Secrets

- `secret`: the webhook secret from step 1. Save it to `/etc/hookspool/secrets/earnings_secret`, mode 0600, owned by
  the hookspool user.
- The API key used for step 2 goes to `/etc/hookspool/secrets/finnhub_api_key`, mode 0600. It is read from that file
  by the commands below and is never printed. Remove the file after the setup if you do not need it again.

## Create and list the webhooks

Run these on the host, one ticker at a time. The key is passed to curl through a config read from stdin, so it does
not appear in the command line or in the URL.

```sh
# create an earnings webhook for one ticker (repeat for each ticker)
printf 'header = "X-Finnhub-Token: %s"\n' "$(cat /etc/hookspool/secrets/finnhub_api_key)" |
  curl -sS -K - -H 'Content-Type: application/json' -X POST https://finnhub.io/api/v1/webhook/add \
    --data '{"event":"earnings","symbol":"AAPL"}'

# list the webhooks the account has (keep the ids for rollback)
printf 'header = "X-Finnhub-Token: %s"\n' "$(cat /etc/hookspool/secrets/finnhub_api_key)" |
  curl -sS -K - https://finnhub.io/api/v1/webhook/list
```

The key goes in the `X-Finnhub-Token` header. Some examples pass it as a `token=` query parameter instead; that puts
the key into the URL and into logs, so use the header form above.

## Config

Add to `config.toml`, then run `hookspool check` and `hookspool reload`:

```toml
[bobbins.earnings]
bobbin = "finnhub.earnings"
secrets.secret = { file = "/etc/hookspool/secrets/earnings_secret" }
outputs = { "*" = "hermes:earnings" }
```

`hookspool bobbin expand earnings` shows the expanded table with the secret as a reference only.

## Proxy

The reverse proxy must forward `POST /hooks/earnings` to the intake unchanged: the body byte for byte, and the
`X-Finnhub-Secret` header. Do not add or strip headers on that path. See `deploy/nginx.example.conf`.

## Verify

Finnhub's earnings webhooks fire when an announcement is published, so a test delivery may not be available on
demand. Check in two steps:

1. Send one captured delivery with the wrong secret and confirm it is refused, and one with the right secret and
   confirm it is queued (`hookspool source test earnings --request <capture>` tests a capture without registering
   anything).
2. When Finnhub sends a real announcement, run `hookspool list --source earnings` and check that the batch shows the
   ticker. Compare the payload with the `data.0.symbol` key path; if the ticker is elsewhere, the key path needs
   changing in the bobbin.

## Roll back

1. Remove `[bobbins.earnings]` from `config.toml`, then run `hookspool check` and `hookspool reload`.
2. Delete the webhooks at Finnhub (use the ids from the list call). The deletion endpoint is not confirmed in the
   pages this bobbin was checked against; use the dashboard if the API call does not work.
3. Remove `/etc/hookspool/secrets/finnhub_api_key` and `/etc/hookspool/secrets/earnings_secret` if they are not
   needed.
