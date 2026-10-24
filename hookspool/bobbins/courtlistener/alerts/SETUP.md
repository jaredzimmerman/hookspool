---
bobbin: courtlistener.alerts
provider: Free Law Project CourtListener alerts (webhooks)
webhook_docs: https://wiki.free.law/c/courtlistener/help/api/webhooks/about
---

# Set up courtlistener.alerts

CourtListener sends its webhook events from two addresses and does not sign them, so this bobbin checks a token
in the endpoint URL that you choose. Before you start, read [README.md](README.md), "Read this first": webhooks
are an agreement-based feature for organizations, and alerts count against your membership.

Pick the instance name `alerts`. The endpoint becomes `/hooks/alerts`.

## Secrets to create

- `secret`: the token that goes in the endpoint URL. Make a long random value, for example
  `openssl rand -hex 32`, and save it to `/etc/hookspool/secrets/alerts_secret`, mode 0600, owned by the hookspool
  user. Nothing else goes in that file.

## Config

Add to `config.toml`, then run `hookspool check` and `hookspool reload`:

```toml
[bobbins.alerts]
bobbin = "courtlistener.alerts"
secrets.secret = { file = "/etc/hookspool/secrets/alerts_secret" }
outputs = { "*" = "hermes:alerts" }
```

## Reverse proxy

Forward `/hooks/alerts` to the intake and keep the query string, because the token is in it. Put this location in
the nginx site that serves your hookspool host (see `deploy/nginx.example.conf`). The `proxy_pass` line has no URI
part, so nginx passes the query string on unchanged.

```nginx
location = /hooks/alerts {
    # The token is in the query string. Log this location with the path-only `hookspool` log_format from
    # deploy/nginx.example.conf: the default combined format writes $request, query string included. Do not add this
    # block to a server that logs in the combined format. Keep the error log for this location at crit so the token
    # stays out of it too.
    access_log /var/log/nginx/hookspool.access.log hookspool;
    error_log /var/log/nginx/hookspool.alerts.error.log crit;

    # Recommended: CourtListener sends only from its two documented addresses (the Webhook Security section of the
    # provider's webhooks guide).
    allow 34.210.230.218;  # hookspool-scan: allow: documented provider addresses
    allow 54.189.59.91;    # hookspool-scan: allow: documented provider addresses
    deny all;

    proxy_pass http://hookspool_intake;
}
```

Then `nginx -t` and `systemctl reload nginx`. If the host sits behind another proxy or a CDN, the address nginx sees
is that proxy's, so the `allow` lines do not match. Remove them in that case and rely on the token.

## Check the config

1. `hookspool check` exits 0, then `hookspool reload` exits 0.
2. Optional, in a scratch config whose secret file holds the test token `test-template-secret-PAYLOAD-SENTINEL-7f3a`:
   `hookspool source test alerts --request hookspool/bobbins/courtlistener/alerts/captures/event.http` prints `queued`,
   and `wrong_token.http` in the same folder prints `unverified`. Run this with the test token only, never with the real
   secret.

## Register the webhook at the provider

Use the provider's webhook panel. The steps are in the provider's guide, "Getting Started With Webhooks":

1. Sign in to CourtListener and open the Webhooks panel in your profile.
2. Click "Add webhook", then enter the endpoint URL `https://<HOOKS_HOST>/hooks/alerts?token=<the secret>`.
   The URL must use HTTPS.
3. Choose the event type (1 to 5, see README.md) and the webhook version. CourtListener allows one endpoint for each
   event type and version, so repeat this for each event type you need, all with the same URL.
4. Leave the enable box unchecked until the config and the proxy are live. The provider advises keeping an endpoint
   disabled until it is live in your application, then click "Create webhook" and enable it in the panel when you are ready.

## Verify

1. In the Webhooks panel, click "Test" on the endpoint and send the JSON test event. Test events are not retried.
2. `hookspool list --source alerts` shows a batch for the test event.
3. A delivery with a wrong token is refused and shows no batch. The `wrong_token.http` capture shows this in the
   check above; do not change the live endpoint URL to test it.

## Roll back

1. Disable or delete each endpoint in the CourtListener Webhooks panel.
2. Remove the `[bobbins.alerts]` table from `config.toml`, then run `hookspool check` and `hookspool reload`.
3. Remove the `location = /hooks/alerts` block from the nginx site, then `nginx -t` and `systemctl reload nginx`.
4. Delete `/etc/hookspool/secrets/alerts_secret` and rotate the token if the URL was ever shared outside the host.
