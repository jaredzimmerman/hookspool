# Trello webhook setup

The Trello bobbin receives board webhooks. Trello signs each delivery with the app secret; the callback
URL is part of the signed input.

## What to create at Trello

1. Create a Trello power-up or API key/app at https://trello.com/power-ups/admin and note the app
   **secret** (the OAuth secret shown with the key). Keep it private.
2. Choose the public callback URL the webhook will use, for example `https://hooks.example.test/hooks/trello`.
   It must be reachable over HTTPS and must end in `/hooks/<instance>`. Write it down exactly.
3. Register the webhook for the board with the Trello REST API
   (`POST https://api.trello.com/1/webhooks/` with `callbackURL`, `idModel` set to the board id, and
   `description`). Trello then sends a HEAD request to the callback URL; hookspool answers it.

## Secrets to save

- Write the app secret to `/etc/hookspool/secrets/trello`, mode 0600, owned by the hookspool user.
  Nothing else is stored in that file.

## Config table

```toml
[bobbins.trello]
settings = { callback_url = "https://hooks.example.test/hooks/trello" }
secrets.secret = { file = "/etc/hookspool/secrets/trello" }
outputs = { "*" = "hermes:trello" }
```

`callback_url` must be byte-for-byte the URL registered in step 3.

## Reverse proxy

Pass `/hooks/trello` through with its path unchanged. Trello sends no query string.

## Verify

1. `hookspool bobbin expand trello` prints the expanded source with the secret as a reference only.
2. `hookspool check`, then `hookspool reload`.
3. Trello's HEAD handshake: `curl -I https://hooks.example.test/hooks/trello` returns 200 with no body.
4. Make a change on the board (add a card). `hookspool list --source trello` shows a queued event.

## Known gap

Until the loader maps instance settings into the template, `callback_url` in the config table is not
passed to the source. `hookspool check` then reports the template as missing `callback_url`, and no
delivery verifies. Do not register the webhook until `hookspool check` is clean.

## Roll back

Delete the `[bobbins.trello]` table, run `hookspool check` and `hookspool reload`. Then delete the webhook
at Trello with `DELETE https://api.trello.com/1/webhooks/<id>`.
