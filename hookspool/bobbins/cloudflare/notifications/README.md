---
bobbin: cloudflare.notifications
provider: Cloudflare Notifications
webhook_docs: https://developers.cloudflare.com/notifications/get-started/configure-webhooks/
---

# cloudflare.notifications

Cloudflare Notifications webhook destination. Cloudflare posts one JSON request per alert to the URL of a
webhook destination. The request carries the destination's secret in the `cf-webhook-auth` header, sent as
typed. This bobbin compares that header with the secret in constant time, refuses a request that does not
match, and queues every other accepted alert for the instance's output. There is no code: it is the template
source with the shared-secret-header scheme.

Alerts that can reach a webhook include Universal SSL, Pages project events, HTTP DDoS attacks, Security
Insights and tunnel health. Which ones are available depends on the account's plan and products. The
complete list is on the [available notifications page](https://developers.cloudflare.com/notifications/notification-available/).

## How it works

- **Secret.** The `cf-webhook-auth` header is compared with the secret in the instance's secret file. The
  body is not covered by the secret, so a change to the body is not detected. Cloudflare's own generic webhook
  documentation recommends a secret, and this bobbin requires one.
- **Payload.** Cloudflare's [payload schema](https://developers.cloudflare.com/notifications/reference/webhook-payload-schema/)
  has `name` (the notification policy's name), `text`, `data`, `ts`, and, when present, `alert_type`,
  `alert_event`, `account_id`, `policy_id`, `policy_name` and `alert_correlation_id`.
- **Key.** The key is `name`, the policy's name. Alerts from one policy in one burst form one batch.
- **Event type.** `name`. The alert type (`alert_type`) and event state (`alert_event`) are attributes, so a
  route can match on them. The template cannot fall back from `alert_type` to `name`, so the event type is
  always the policy name.
- **Dedup.** The SHA-256 of the raw body, because the payload has no delivery id. A redelivery of the same
  body is recognised. Separate alerts usually differ in `ts` and `alert_correlation_id`, so their bodies differ.
- **Span.** `burst`, so a flurry of alerts coalesces.

## Settings

None. The bobbin needs no settings.

## Secrets

| name | meaning |
|---|---|
| `secret` | the secret typed into the webhook destination, which Cloudflare sends in `cf-webhook-auth` |

## Outputs

`"*"`: every accepted alert goes to the one mapped destination. The alert type and event state are in the
attributes.

## Limits

- Cloudflare delivers generic webhooks only to a publicly resolvable IP address, on port 80 or 443. A
  private address or another port needs a Worker or a Cloudflare Tunnel in front of the endpoint.
- Cloudflare sends from its published IP ranges. If the endpoint sits behind a firewall, allow those ranges.
- The pages checked do not document redelivery of a failed generic webhook. `sender_retries` is left unset, no
  sweep is defined, and `expect_every_s` is not set (SETUP.md, Retries).
- A test message from **Save and Test** has no `name` field, because Cloudflare's example body is text only.
  Hookspool cannot key it, so it is quarantined every time. That is expected, and it does not mean the secret
  is wrong.
- Only the `cf-webhook-auth` header is checked. The body is not signed, so anyone who has the secret can send
  any alert.

## Captures

`captures/event.http` (an alert with the right secret, queued) and `captures/wrong_secret.http` (the same alert
with another secret, unverified). They are sent with the test secret in `captures/kit.toml`. The payload is
made up, and its values are placeholders.
