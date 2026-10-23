# Zoom event subscription setup

The zoom bobbin receives Zoom event subscriptions. Zoom signs each delivery with the app's Secret Token.

## What to create at Zoom

1. In the Zoom App Marketplace, open (or create) the app whose events you want, and open its **Features**
   page, section **Event Subscriptions**.
2. Note the app's **Secret Token** shown there. Keep it private.
3. Choose the public endpoint URL, for example `https://hooks.example.test/hooks/zoom`. It must be reachable
   over HTTPS and end in `/hooks/<instance>`.

## Secrets to save

- Write the Secret Token to `/etc/hookspool/secrets/zoom_webhook_secret`, mode 0600, owned by the hookspool
  user. Nothing else is stored in that file.

## Config table

```toml
[bobbins.zoom]
secrets.webhook_secret = { file = "/etc/hookspool/secrets/zoom_webhook_secret" }
outputs = { "*" = "hermes:zoom" }
```

## Reverse proxy

Pass `/hooks/zoom` through with its path, body and the `x-zm-request-timestamp` and `x-zm-signature` headers
unchanged.

## Apply

1. `hookspool bobbin expand zoom` prints the expanded source with the secret as a reference only.
2. `hookspool check`, then `hookspool reload`. Do this before the endpoint is added at Zoom: the validation
   is answered only when the secret is already saved.

## Add the endpoint at Zoom

3. In the app's Event Subscriptions, add a subscription with the endpoint URL from step 3 above, choose the
   events to receive, and press **Validate**. Zoom sends `endpoint.url_validation` and enables the subscription
   when the answer is right. Save.

## Verify

1. Hold or end a test meeting. `hookspool list --source zoom` shows a queued event whose type is the event name.
2. A delivery with a wrong signature or an old timestamp is refused and does not appear in the list.

## Roll back

Delete the `[bobbins.zoom]` table, run `hookspool check` and `hookspool reload`. Then remove or disable the
subscription at Zoom, or Zoom will keep posting to an endpoint that answers 404.
