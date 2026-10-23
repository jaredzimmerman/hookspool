# Plaid webhook setup

The plaid bobbin receives Plaid webhooks. Plaid signs each with an ES256 JWT; hookspool checks it against the
keys Plaid publishes, fetched with your client id and secret.

## What to create at Plaid

1. In the Plaid Dashboard, open the API keys of the environment you use (Sandbox, Development or Production)
   and note the **client_id** and the **secret** for that environment. Keep both private.
2. Choose the public webhook URL, for example `https://hooks.example.test/hooks/plaid`. It must be reachable
   over HTTPS and end in `/hooks/<instance>`.

## Secrets to save

- Write the client id to `/etc/hookspool/secrets/plaid_client_id` and the secret to
  `/etc/hookspool/secrets/plaid_secret`, each mode 0600, owned by the hookspool user. Nothing else is stored in
  those files.

## Config table

```toml
[bobbins.plaid]
secrets.client_id = { file = "/etc/hookspool/secrets/plaid_client_id" }
secrets.secret = { file = "/etc/hookspool/secrets/plaid_secret" }
settings = { env = "production" }          # or "sandbox" / "development", matching the keys above
outputs = { "*" = "hermes:plaid" }
```

## Reverse proxy

Pass `/hooks/plaid` through with its path, body and the `Plaid-Verification` header unchanged. Keep a per-client
`limit_req` on the path (`deploy/nginx.example.conf`): unauthenticated requests are cheap to refuse, but each
unknown key id can cause one key fetch a minute.

## Apply

1. `hookspool bobbin expand plaid` prints the expanded source with the secrets as references only.
2. `hookspool check`, then `hookspool reload`.

## Point Plaid at the URL

3. Set the webhook URL where Plaid takes it: the `webhook` field when you create a Link token, or
   `/item/webhook/update` for an existing item (and the Sandbox `/sandbox/item/fire_webhook` to send a test).

## Verify

1. Fire a test webhook (Sandbox) or let one arrive. `hookspool list --source plaid` shows a queued event whose
   type is `<webhook_type>.<webhook_code>`.
2. A webhook with a wrong signature, an old `iat` or a changed body is refused and does not appear in the list.
   If every webhook is refused, check `env` against the environment the keys belong to, and the logs for
   `plaid <env> key fetch failed`.

## Roll back

Delete the `[bobbins.plaid]` table, run `hookspool check` and `hookspool reload`. Then point the items back at
their old webhook URL (or clear it) at Plaid, or Plaid will keep posting to an endpoint that answers 404.
