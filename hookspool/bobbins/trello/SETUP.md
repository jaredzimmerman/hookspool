# Trello webhook setup

The Trello bobbin receives board webhooks. Trello signs each delivery with the app secret; the callback
URL is part of the signed input.

## What to create at Trello

1. Create a Trello API key and app at https://trello.com/power-ups/admin and note the app **secret**
   (the OAuth secret shown with the key). Keep it private.
2. Choose the public callback URL the webhook will use, for example `https://hooks.example.test/hooks/trello`.
   It must be reachable over HTTPS and must end in `/hooks/<instance>`. Write it down exactly.
3. Note the id of the board to watch.

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

`callback_url` must be byte-for-byte the URL registered in step 4 below. Without it the expansion fails
(`settings.callback_url: required`).

## Reverse proxy

Pass `/hooks/trello` through with its path unchanged. Trello sends no query string.

## Apply and check the handshake

1. `hookspool bobbin expand trello` prints the expanded source with the secret as a reference only.
2. `hookspool check`, then `hookspool reload`. Do not register the webhook until the check is clean.
3. `curl -I https://hooks.example.test/hooks/trello` returns HTTP 200 with no body. Trello runs the same
   HEAD request when the webhook is created, and creation fails on anything else.

## Register the webhook

4. With your own Trello API key and token, register the webhook with the Trello REST API:
   `POST https://api.trello.com/1/webhooks/` with the form fields `callbackURL` (the exact URL from step 2),
   `idModel` (the board id) and `description`, authenticated with your key and token. Keep the key and
   token to yourself; they are not part of hookspool's configuration.

## Verify

1. Make a change on the board (add a card). `hookspool list --source trello` shows a queued event for the
   board.
2. A delivery with the wrong signature is refused. `hookspool list --source trello` shows no event for it.
   If the board's events never arrive, compare the `callbackURL` on the webhook at Trello with the config.

## Roll back

Delete the `[bobbins.trello]` table, run `hookspool check` and `hookspool reload`. Then delete the webhook
at Trello with `DELETE https://api.trello.com/1/webhooks/<id>`, authenticated with your key and token.
