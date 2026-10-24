---
bobbin: kaneo.projects
provider: Kaneo Project webhooks
webhook_docs: https://kaneo.app/docs/core/integrations/outgoing-webhooks
---

# kaneo.projects

Kaneo generic webhook. Kaneo posts project and task events (task created, status changed, and so on) as
a JSON envelope to the URL set on its generic webhook integration. This bobbin verifies the HMAC-SHA256
signature over the JSON body and queues each accepted event for the instance's output. There is no code:
it is the template source with Kaneo's signature rules.

## How it works

- **Signature.** `X-Kaneo-Signature` is the lowercase hex HMAC-SHA256 of the request body, with no prefix.
  The body is checked as received.
- **Envelope.** The body is `{event, timestamp, project, task, actor, data}`. The event name is `event`.
- **Key and dedup.** The key is `project.id`. Kaneo sends no delivery id, so the dedup id is the hash of
  the body: an identical redelivery is recognised, and two different events are not merged.
- **Span.** `mail`, the default preset.

## Settings

None.

## Secrets

| name | meaning |
|---|---|
| `secret` | the secret set on the Kaneo generic webhook integration |

## Outputs

`"*"`: every accepted event goes to the one mapped destination. The event name and project are in the
attributes.

## Limits

- Whether Kaneo retries a failed delivery is not documented, so `sender_retries` is left unset. Until it
  is known, plan for a missed event and check the Kaneo side for failed deliveries.
- A body edit by any proxy changes the hash and fails verification; the proxy must pass the body unchanged.

## Captures

`captures/event.http` (a `task.created` event, accepted, queued) and `captures/tampered.http` (the body
changed after signing, unverified). They are signed with the test secret.
