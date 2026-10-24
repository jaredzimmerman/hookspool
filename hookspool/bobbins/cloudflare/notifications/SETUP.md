---
bobbin: cloudflare.notifications
provider: Cloudflare Notifications
webhook_docs: https://developers.cloudflare.com/notifications/get-started/configure-webhooks/
---

# Cloudflare Notifications setup

The Cloudflare Notifications bobbin receives alerts from a Cloudflare webhook destination. Each destination has
its own secret; use one instance per destination.

## What to create

1. Choose the destination's purpose and name the instance, for example `cloudflare-alerts`.
2. Generate a random secret, for example `openssl rand -hex 32`. Keep it private.
3. Choose the public callback for the instance, for example `https://hooks.example.test/hooks/cloudflare-alerts`.
   It must be reachable on port 443 with a public address.

## Secrets to save

- Write the secret to `/etc/hookspool/secrets/cloudflare-alerts`, mode 0600, owned by the hookspool user.

## Config table

```toml
[bobbins.cloudflare-alerts]
bobbin = "cloudflare.notifications"
secrets.secret = { file = "/etc/hookspool/secrets/cloudflare-alerts" }
outputs = { "*" = "hermes:cloudflare-alerts" }
```

## Reverse proxy

Pass `/hooks/cloudflare-alerts` through with the path unchanged. The callback URL has no query string. To check it,
send a request with no secret header to the callback. It must return 401, not 404 or 502.

## Apply, then create the destination

1. `hookspool bobbin expand cloudflare-alerts` prints the source with the secret as a reference only.
2. `hookspool check`, then `hookspool reload`. Do this before creating the destination, so the first alert
   is accepted.
3. In the Cloudflare dashboard, go to Alerts, then Destinations. In the Webhooks card, select Create. Enter a
   name, the callback URL, and the secret from `/etc/hookspool/secrets/cloudflare-alerts`. Select Save and Test.
4. In Alerts, create a notification policy for the alerts you want, and add the webhook destination to it.

## Verify

1. Save and Test sends a test message. Cloudflare's example body is text only, with no `name` field, so hookspool
   quarantines it every time. The quarantine is expected, and it does not mean the secret is wrong. A 401 means the
   header differs from the secret file.
2. Wait for a real alert, or use one the policy sends, and run `hookspool list --source cloudflare-alerts`.
   It shows one queued event.

## Retries

Cloudflare does not document whether it retries a failed generic webhook delivery. Until that is confirmed, treat
the sender as non-retrying: an alert that arrives while hookspool is down may be lost. No sweep is defined for this
source, and `sender_retries` is left unset. Confirm the behaviour with Cloudflare, or test it with a deliberate
failure, before you rely on the integration. If Cloudflare does not retry, ask the operator for a sweep before
go-live. `expect_every_s` is not set, so hookspool does not open a silence issue for this source.

## Roll back

Delete the `[bobbins.cloudflare-alerts]` table, run `hookspool check` and `hookspool reload`, then delete the
webhook destination in the Cloudflare dashboard and remove it from any notification policy.
