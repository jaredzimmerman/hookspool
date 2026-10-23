# trello

Trello board webhook. Trello posts an action (card created, card updated, comment added and so on) to
the URL the webhook was registered with. This bobbin verifies the signature, answers Trello's unsigned
HEAD handshake, and queues each accepted action for the instance's output. There is no code: it is the
template source (`[source.template]`) with Trello's signature rules.

## How it works

- **Signature.** `X-Trello-Webhook` is base64(HMAC-SHA1(app secret, body + callback URL)). The callback
  URL is part of the signed input, byte for byte the URL the webhook was registered with. So the
  instance must give that URL exactly (setting `callback_url`).
- **Handshake.** When a webhook is created Trello sends an unsigned `HEAD` request and expects an empty
  200. The source answers it with a constant response that never touches the secret.
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
- The loader does not yet pass `[settings]` into `[source.template]` (ARCHITECTURE 14.3.2a). Until it
  does, the instance's `callback_url` does not reach the source, and `hookspool check` reports the
  template as missing it. See SETUP.md.
- Trello does not document a retry policy in the manifest, so `sender_retries` is left unset.

## Captures

`captures/event.http` (accepted, queued), `captures/tampered.http` (body changed after signing, so
unverified), `captures/head.http` (the HEAD handshake, challenge). They are signed with the test secret
and the test callback URL `https://hooks.example.test/hooks/trello`.
