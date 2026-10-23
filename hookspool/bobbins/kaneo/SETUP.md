# Kaneo webhook setup

The Kaneo bobbin receives events from Kaneo's generic webhook integration. Kaneo signs the JSON body with
a shared secret.

## What to create at Kaneo

1. Open the project's integrations in Kaneo and add a **generic webhook** integration.
2. Set the URL to the public callback, for example `https://hooks.example.test/hooks/kaneo`.
3. Set a secret. Use a random value, for example `openssl rand -hex 32`, and keep it private.
4. Choose the events to send (task created, status changed, and so on) and save.

## Secrets to save

- Write the secret to `/etc/hookspool/secrets/kaneo`, mode 0600, owned by the hookspool user.

## Config table

```toml
[bobbins.kaneo]
secrets.secret = { file = "/etc/hookspool/secrets/kaneo" }
outputs = { "*" = "hermes:kaneo" }
```

## Reverse proxy

Pass `/hooks/kaneo` through and do not rewrite the body. The signature covers the body byte for byte.

## Verify

1. `hookspool bobbin expand kaneo` prints the source with the secret as a reference only.
2. `hookspool check`, then `hookspool reload`.
3. Create or change a task in the project. `hookspool list --source kaneo` shows a queued event.
4. A tampered body is refused: `hookspool source test kaneo --request <capture>` reports `unverified`.

## Roll back

Delete the `[bobbins.kaneo]` table, run `hookspool check` and `hookspool reload`, then delete the
integration in Kaneo.
