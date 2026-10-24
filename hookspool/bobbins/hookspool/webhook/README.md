---
bobbin: hookspool.webhook
provider: hookspool Generic webhook
webhook_docs: https://github.com/jaredzimmerman/hookspool/blob/main/docs/bobbins/webhook.md
---

# hookspool.webhook

The bobbin for any web service that has no bobbin of its own and sends a signed or tokened JSON POST. There
is no code in it: the instance's settings are the template source's keys, so one `[bobbins.<instance>]` table
describes how the sender proves itself, what an event coalesces on, which header carries a delivery id, and
what to ignore. The settings are checked exactly as a template source is checked.

It is also the scaffold for a dedicated bobbin: `hookspool bobbin new <vendor>.<product>` copies it and freezes the
settings into a fixed `[source] template`. When several deployments set up the same service through
`hookspool.webhook`, give that service its own bobbin.

## How it works

- **Scheme.** One of five. The setting `scheme` says how the sender proves it is the sender:

  | scheme | the sender sends | settings it uses |
  |---|---|---|
  | `hmac-sha256-hex` | the hex HMAC-SHA256 of the raw body, optionally behind a prefix | `header`, `prefix` |
  | `hmac-sha256-base64` | the same, base64 encoded | `header`, `prefix` |
  | `hmac-sha1-base64-url` | base64 HMAC-SHA1 of the body followed by the callback URL | `header`, `callback_url` |
  | `shared-secret-header` | the secret itself in a header, optionally behind a prefix | `header`, `prefix` |
  | `url-token` | the secret as a query parameter | `param` |

  Checks are constant time and cover the body exactly as received. `url-token` puts the secret in the URL;
  use a signature scheme when the sender offers one, and keep the query string in the reverse proxy.
- **Key.** `key` is what events coalesce on: a payload path (`a.b.0.c`), `header:Name` or `literal:text`.
  Give a list to join several with `:`. A key that cannot be read quarantines the request instead of losing it.
- **Delivery id.** `dedup` names the field that recognises a redelivery (a payload path, `header:Name` or
  `body_sha256`).
- **Ignore.** Each `ignore` entry is `<payload path or header:Name>=<value>`; a matching delivery is
  verified, acknowledged with 200 and not queued (a ping, for example).
- **Event type and attributes.** `event_type` reads the event type; `attrs` copies named values onto the
  event so a destination can route on them.
- **Handshake.** `head_handshake = true` answers the sender's unsigned `HEAD` probe with 200.
- **Retries.** `sender_retries = true` says the sender retries a failed delivery by itself. It changes nothing
  in how deliveries are handled; it only decides whether `hookspool check` warns that the sender does not retry
  (the default, `false`), in which case a sweep must redeliver what was lost while hookspool was down
  (SETUP.md, "Sweep").

## Settings

Each setting is a key of the template source, with its own meaning there (`hookspool bobbin show hookspool.webhook`
lists them with their types).

| setting | type | |
|---|---|---|
| `scheme` | string | required |
| `key` | list | required |
| `header`, `prefix`, `param`, `callback_url` | string | what the scheme needs |
| `dedup`, `event_type` | string | optional |
| `ignore` | list | optional |
| `attrs` | table | optional |
| `head_handshake`, `sender_retries` | bool | default false |

A value the template refuses fails `hookspool check` with the template's own message, for example
`header: required by scheme hmac-sha256-hex`. An empty string, list or table means unset, whether the
instance gives it or it is the default (`dedup = ""` is the same as leaving `dedup` out).

## Secrets

| name | meaning |
|---|---|
| `secret` | the signing secret or token the sender gives you for the webhook |

## Outputs

`"*"`: every accepted delivery goes to the one mapped destination. To route by event type, map further outputs
named after the event types `event_type` produces; each becomes a route on `event_type`. The routes read the
event's attributes, so the instance must also copy the type there: `attrs = { event_type = "<the same selector>" }`.
`hookspool check` reports an error when outputs are mapped per event type and `attrs.event_type` is missing
(otherwise every event would silently reach `"*"`).

## Limits

- It reads JSON bodies. A sender that signs something other than the raw body, or needs a handshake
  beyond `HEAD`, needs a code bobbin (`hookspool bobbin new <name> --code`).
- Selectors read one value each; there is no expression language.

## Captures

`captures/event.http` (accepted, queued), `captures/ping.http` (verified and ignored),
`captures/tampered.http` and `captures/bad_signature.http` (unverified) are signed with the test secret for the
instance in `captures/kit.toml`. `captures/schemes/` holds one accepted delivery per scheme; the settings each
one is read with are in `tests/bobbins/test_hookspool_webhook.py`.
