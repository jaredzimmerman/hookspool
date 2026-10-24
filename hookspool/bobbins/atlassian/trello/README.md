---
bobbin: atlassian.trello
provider: Atlassian Trello
webhook_docs: https://developer.atlassian.com/cloud/trello/guides/rest-api/webhooks/
---

# atlassian.trello

Trello board webhook. Trello posts an action (card created, card updated, comment added and so on) to
the URL the webhook was registered with. This bobbin verifies the signature, answers Trello's unsigned
HEAD handshake, and queues each accepted action for the instance's output. There is no code: it is the
template source (`[source.template]`) with Trello's signature rules.

## How it works

- **Signature.** `X-Trello-Webhook` is base64(HMAC-SHA1(app secret, body + callback URL)). The callback
  URL is part of the signed input, byte for byte the URL the webhook was registered with. So the
  instance must give that URL exactly (setting `callback_url`), and the setting reaches the source as the
  template's `callback_url` key.
- **Handshake.** When a webhook is created Trello sends an unsigned `HEAD` request to the callback URL and
  creates the webhook only if the answer is HTTP 200. The source answers it with a constant response that
  never touches the secret, so the instance must be running and checked before the webhook is created.
- **Key and dedup.** The key is `model.id` (the board), the dedup id is `action.id`, so a re-sent action
  is recognised. The event type is `action.type` (for example `updateCard`); it is an attribute.
- **Span.** `mail` (the default preset), which is the window used to coalesce bursts of actions.

## Settings

| name | type | required | meaning |
|---|---|---|---|
| `callback_url` | string | yes | the exact callback URL the webhook was registered with |

## Secrets

| name | meaning |
|---|---|
| `secret` | the Trello app secret that signs deliveries |

## Outputs

`"*"`: every accepted Trello action goes to the one mapped destination. The event type is in the
`event` attribute.

## Limits

- `callback_url` must match the registered URL exactly, including scheme, host and path. A mismatch
  makes every delivery fail verification (`unverified`).
- Trello retries a failed callback 3 times, about 30, 60 and 120 seconds after the first failure
  (`sender_retries = true`). An outage of hookspool longer than that can lose deliveries; Trello documents
  no sweep for them.
- Trello documents that it disables a webhook after consecutive failures for 30 days and more than 1000
  failures (both are needed), and that these limits may change. If events stop, check the webhook at Trello.

## Captures

`captures/event.http` (accepted, queued), `captures/tampered.http` (body changed after signing, so
unverified), `captures/head.http` (the HEAD handshake, challenge). They are signed with the test secret
and the test callback URL `https://hooks.example.test/hooks/trello`.
