---
bobbin: courtlistener.alerts
provider: Free Law Project CourtListener alerts (webhooks)
webhook_docs: https://wiki.free.law/c/courtlistener/help/api/webhooks/about
---

# Set up courtlistener.alerts (for an agent)

The same procedure as SETUP.md, as steps. Read README.md, "Read this first", before you start.

## Needs from the human

- A CourtListener account that may create webhook endpoints. Webhooks are an agreement-based feature for organizations:
  ask the human to confirm the agreement is in place before step 3.
- The token `secret`, which the human chooses and writes to `/etc/hookspool/secrets/alerts_secret` (mode 0600, owned by
  the hookspool user). Never pass its value to the agent.
- The public HTTPS host that serves hookspool, for the endpoint URL (`<HOOKS_HOST>` below).

## Steps

1. **Add the config table.** Goal: hookspool accepts the deliveries. Add to `config.toml`, then run `hookspool check` and
   `hookspool reload`:

   ```toml
   [bobbins.alerts]
   bobbin = "courtlistener.alerts"
   secrets.secret = { file = "/etc/hookspool/secrets/alerts_secret" }
   outputs = { "*" = "hermes:alerts" }
   ```

   Check: `hookspool check` exits 0, then `hookspool reload` exits 0 (without the reload the running intake does not know the
   table). If `hookspool check` fails: fix what it names, then run both again.
2. **Forward the path at the reverse proxy** (host change: ask the human first). Goal: `/hooks/alerts` reaches the intake with
   its query string, and only CourtListener's two addresses reach it. Add the `location = /hooks/alerts` block from SETUP.md
   to the nginx site, with its `allow` lines (replace the two `<COURTLISTENER_IP_n>` placeholders with the addresses from the
   provider's webhook security section first), then run `nginx -t` and `systemctl reload nginx`. If the host sits behind
   another proxy, drop the `allow` lines.
   Check: `nginx -t` exits 0 and the reload succeeds. If not: report the error and stop.
3. **Register the webhook at CourtListener** (`outward`: ask the human first). Goal: the provider sends to
   `https://<HOOKS_HOST>/hooks/alerts?token=<the secret>`, one endpoint per event type (1 to 5) that the human needs, all with
   that URL. Do this in the provider's Webhooks panel, following SETUP.md, "Register the webhook at the provider". Leave
   "enable" unchecked until steps 1 and 2 are live.
   Check: the panel lists each endpoint with the right event type and URL. If not: report what the panel shows.
4. **Verify.** Goal: one delivery arrives. Ask the human to send a test event from the endpoint's "Test" button, then run
   `hookspool list --source alerts`.
   Check: a batch is listed. If not: run `hookspool status` and report it.
5. **Roll back** (`outward` for the provider part): disable or delete each endpoint in the Webhooks panel, remove the
   `[bobbins.alerts]` table and run `hookspool check` and `hookspool reload`, remove the nginx `location` block and reload
   nginx, then delete `/etc/hookspool/secrets/alerts_secret`.
