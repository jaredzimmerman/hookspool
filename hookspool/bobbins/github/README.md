# github

GitHub repository webhook. GitHub posts a delivery (push, pull request, issue, workflow run and so on)
to the URL the webhook is set to. This bobbin verifies the HMAC-SHA256 signature over the raw body,
acknowledges `ping` deliveries without queuing them, and queues every other accepted delivery for the
instance's output. There is no code: it is the template source with GitHub's signature rules.

Two instances are the usual setup: one per webhook, each with its own secret, for example `github-ci`
(workflow and push events for a CI agent) and `github-prs` (pull request events). Each is a
`[bobbins.<instance>]` table with `bobbin = "github"`. They are configuration, not two bobbins.

## How it works

- **Signature.** `X-Hub-Signature-256` is `sha256=` plus the hex HMAC-SHA256 of the raw body with the
  webhook secret. The body is checked as received; it is not re-serialised.
- **Ping.** `X-GitHub-Event: ping` is verified, acknowledged with 200 and never queued. The `ignore`
  rule covers it.
- **Key and dedup.** The key is `repository.full_name`. The dedup id is `X-GitHub-Delivery`, so a
  redelivery of the same delivery is recognised.
- **Event type.** `X-GitHub-Event` (`push`, `pull_request`, ...). It is also an attribute, and the
  event header is kept so the destination can route on it.
- **Span.** `burst`, so a flurry of pushes coalesces.

## Settings

None. The bobbin needs no settings.

## Secrets

| name | meaning |
|---|---|
| `secret` | the webhook secret typed into the repository's webhook settings |

## Outputs

`"*"`: every accepted delivery goes to the one mapped destination. The event type and repository are in
the attributes.

## Limits

- GitHub does not retry a failed delivery. `sender_retries = false`: a sweep must redeliver failures
  (GitHub's "Redeliver" in the webhook's recent deliveries). Without it, a failed delivery is lost.
- Only the event, delivery and repository headers are kept. The body is not logged.

## Captures

`captures/event.http` (a push, accepted, queued), `captures/tampered.http` (a changed body, unverified),
`captures/ping.http` (a ping, verified and ignored). They are signed with the test secret; the
repository in the captures is `octo/widgets`.
