---
bobbin: finnhub.earnings
provider: Finnhub Webhooks
webhook_docs: https://finnhub.io/docs/api/webhook
---

# Set up finnhub.earnings

This bobbin receives Finnhub's earnings webhook. Finnhub's documentation for it is thin (see the README), so the
steps below follow the pattern other integrations use. Each unconfirmed point is marked, and each one must be checked
on the first real delivery.

## What to set at Finnhub

1. Sign in to your Finnhub account and find your API key and the webhook secret the account shows for webhooks.
   The official page does not say where the secret is shown. The Webhooks section of the dashboard is where
   third-party guides point. **Unconfirmed.**
2. Find where the account sets the destination URL and the secret for webhooks, and set the URL to
   `https://<your-hooks-host>/hooks/earnings`. **Unconfirmed:** the pages checked do not say where this is set. The
   `webhook/add` call below carries only `event` and `symbol`, so the URL is not sent with it. If the account has no
   such setting, stop here and report it. Do not create webhooks without a destination.
3. Finnhub's REST reference is at <https://finnhub.io/docs/api>. The webhook endpoints are in the API reference or the
   Webhooks section of the dashboard. Confirm which before creating anything.

## Secrets

- `secret`: the webhook secret from step 1. Save it to `/etc/hookspool/secrets/earnings_secret`, mode 0600, owned by
  the hookspool user.
- The API key used for the webhook calls goes to `/etc/hookspool/secrets/finnhub_api_key`, mode 0600. It is read from
  that file by the commands below and is never printed. Remove the file after the setup if you do not need it again.

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

## Verify the intake before any webhook exists

Check the intake and the proxy before you create a webhook at Finnhub. The bobbin does not assume the sender retries,
so an announcement that arrives while the path is broken may not come again.

1. Send one captured delivery with the wrong secret and confirm it is refused, and one with the right secret and
   confirm it is queued. `hookspool source test earnings --request <capture>` tests a capture without registering
   anything. Repeat the same two checks through the public URL, so the proxy is covered too.

## Create and list the webhooks

Run these on the host, one ticker at a time, only after the intake check above passes and the destination in step 2
is set. The key is passed to curl through a config read from stdin, so it does not appear in the command line or in
the URL.

```sh
# create an earnings webhook for one ticker (repeat for each ticker)
printf 'header = "X-Finnhub-Token: %s"\n' "$(cat /etc/hookspool/secrets/finnhub_api_key)" |
  curl -sS -K - -H 'Content-Type: application/json' -X POST https://finnhub.io/api/v1/webhook/add \
    --data '{"event":"earnings","symbol":"AAPL"}'

# list the webhooks the account has (keep the ids for rollback)
printf 'header = "X-Finnhub-Token: %s"\n' "$(cat /etc/hookspool/secrets/finnhub_api_key)" |
  curl -sS -K - https://finnhub.io/api/v1/webhook/list
```

The key goes in the `X-Finnhub-Token` header. Some examples pass it as a `token=` query parameter instead. That puts
the key into the URL and into logs, so use the header form above.

## Confirm a real delivery

Finnhub's earnings webhooks fire when an announcement is published, so a test delivery may not be available on
demand. When Finnhub sends a real announcement, run `hookspool list --source earnings` and check that the batch shows
the ticker. Compare the payload with the `data.0.symbol` key path. If the ticker is elsewhere, the key path needs
changing in the bobbin.

## Roll back

1. Remove `[bobbins.earnings]` from `config.toml`, then run `hookspool check` and `hookspool reload`.
2. Delete the webhooks at Finnhub (use the ids from the list call). The deletion endpoint is not confirmed in the
   pages this bobbin was checked against; use the dashboard if the API call does not work.
3. Remove `/etc/hookspool/secrets/finnhub_api_key` and `/etc/hookspool/secrets/earnings_secret` if they are not
   needed.
