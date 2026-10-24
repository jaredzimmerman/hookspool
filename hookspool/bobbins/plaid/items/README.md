---
bobbin: plaid.items
provider: Plaid Items
webhook_docs: https://plaid.com/docs/api/webhooks/
---

# plaid.items

Plaid webhooks (item, transactions, link and the other products). Plaid posts each webhook to the URL set
when the Link token or the item was created. This bobbin verifies Plaid's ES256 signature, and queues each
accepted webhook for the instance's output. There is no processor: the webhooks go straight to the
destination mapped to `"*"`. Plaid's webhook bodies carry no data, only the item and the reason to look, so
the destination is what goes and reads the data.

## How it works

- **Signature.** Plaid sends an ES256 JWT in `Plaid-Verification`. Its `kid` names a public key that
  hookspool fetches from Plaid's `/webhook_verification_key/get`, authenticated with the client id and
  secret. The verification, in order: the token's structure and `alg == ES256` (offline,
  `hookspool.verify.jwt_header`); the key for the `kid` (below); the signature, `iat` within five minutes
  either side of now, and the `request_body_sha256` claim equal to the SHA-256 of the raw body
  (`hookspool.verify.jwt_es256`).
- **Key fetch guard.** The keys come through `hookspool.verify.KeyCache`: known keys are cached for 24 hours,
  an unknown `kid` is remembered as unknown for 10 minutes, and at most one fetch is made a minute per `env`
  for the whole process, so unauthenticated requests with random `kid`s cannot amplify against Plaid's API or
  tie up intake threads. A request that needs a fetch inside that minute is answered 401 and Plaid retries it
  with backoff, so a genuine key rotation is picked up within a minute. A known key whose 24 hours have run
  out is still served while a refresh is blocked or failing, so a client holding the fetch slot cannot starve
  genuine webhooks; it is dropped only when Plaid says it is unknown or expired. The cache is shared by every
  Plaid instance in the same `env`. A bad client id or secret (Plaid answers `INVALID_API_KEYS`) never drops
  a known key. A failed fetch leaves one log line with the HTTP status and Plaid's error code, never a body
  or a credential. If the credentials cannot be read, the answer is 503 (retry) and not 401.
- **Key and dedup.** The key is `item_id` (`link_token` for link events, which have no item; the webhook
  type otherwise). Plaid bodies carry no id and are byte-identical for every `SYNC_UPDATES_AVAILABLE`
  notification on an item, so the dedup id is the SHA-256 of the body plus the 600 second bucket it arrived
  in: a retry of one delivery inside its bucket is a duplicate, an identical body in a later bucket is a new
  event (at a bucket edge a retry can be delivered twice, never lost).
- **Span.** `sync`, which groups the bursts a bank backfill sends.
- **Event type and attributes.** The event type is `<webhook_type>.<webhook_code>`. Route `match` tables can
  use `webhook_type`, `webhook_code` and `environment`.

## Requirements

The `plaid` extra of hookspool (PyJWT and cryptography): `pip install '.[plaid]'` in the hookspool checkout, or with
pipx `pipx install '.[plaid]'` or `pipx inject hookspool cryptography PyJWT`. Without it every
webhook answers 503 and `hookspool check` does not warn; test with `python -c "import jwt, cryptography"`.

## Settings

| name | type | required | meaning |
|---|---|---|---|
| `env` | string | no (default `production`) | `production`, `development` or `sandbox`: which Plaid host the keys are fetched from; any other value is refused by `hookspool check` |

## Secrets

| name | meaning |
|---|---|
| `client_id` | the Plaid client id; used only to fetch verification keys |
| `secret` | the Plaid secret for the environment; used only to fetch verification keys |

## Outputs

`"*"`: every accepted webhook goes to the one mapped destination.

## Limits

- A `kid` never cached (a key rotation, or the first key after an intake restart) still competes for the one
  fetch a minute: a client sending random-`kid` requests about once a second can hold that key at 401 until it
  stops or Plaid stops retrying. The reverse proxy's per-client `limit_req` slows that down.
- Plaid retries failed webhooks with backoff over hours (`sender_retries = true`).
- The client id and secret belong to one `env`; use one instance per environment.

## Captures

`captures/sync_updates.http` (a signed `TRANSACTIONS.SYNC_UPDATES_AVAILABLE`, queued), `captures/stale.http`
(signed too long before it arrived, unverified) and `captures/tampered.http` (body changed after signing,
unverified). They carry a JWT signed with a test key; `captures/kit.toml` holds that key's public half, which
the conformance kit serves as Plaid's answer to the key request.
