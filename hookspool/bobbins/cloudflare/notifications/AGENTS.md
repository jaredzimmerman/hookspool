---
bobbin: cloudflare.notifications
provider: Cloudflare Notifications
webhook_docs: https://developers.cloudflare.com/notifications/get-started/configure-webhooks/
---

# Cloudflare Notifications webhook: agent setup

Same procedure as SETUP.md, as steps. Read the **Needs from the human** list first. Repeat steps 1 to 6 for each
instance. The agent never receives the webhook secret: the person writes the secret file and creates the
destination in Cloudflare.

## Needs from the human

- The instance name (`<INSTANCE>`) and the public HTTPS callback URL for it (`<CALLBACK_URL>`).
- The webhook secret, written by the person into `/etc/hookspool/secrets/<INSTANCE>` with mode 0600 (step 1).
  The agent does not read it, print it or repeat it.
- Approval before the destination is created in Cloudflare (step 3) and before a notification policy uses it.

## Steps

1. **Goal:** the secret file exists. The person writes the secret to `/etc/hookspool/secrets/<INSTANCE>`.
   **Check:** `test -s /etc/hookspool/secrets/<INSTANCE> && ls -l /etc/hookspool/secrets/<INSTANCE>` shows mode
   `-rw-------`. **If it fails:** ask the person to write the file again.
2. **Goal:** the instance table is in the config, loaded and valid. Add this table, with `cloudflare-alerts` replaced
   everywhere in it by the instance name `<INSTANCE>`:
   ```toml
   [bobbins.cloudflare-alerts]
   bobbin = "cloudflare.notifications"
   secrets.secret = { file = "/etc/hookspool/secrets/cloudflare-alerts" }
   outputs = { "*" = "hermes:cloudflare-alerts" }
   ```
   Then run `hookspool bobbin expand <INSTANCE>` (it must succeed and show the secret as a reference only),
   `hookspool check`, and `hookspool reload`. **Check:** no error mentions `<INSTANCE>`. **If it fails:** fix the
   table, do not change the bobbin.
3. **Outward, human-performed:** the person creates the webhook destination in the Cloudflare dashboard, under
   Alerts, then Destinations, in the Webhooks card, with the URL `<CALLBACK_URL>` and the secret from step 1.
   The agent does not run this. Ask the person to confirm when it is done.
4. **Goal:** an alert is queued. Ask the person to wait for an alert, or to trigger one the policy sends, then run
   `hookspool list --source <INSTANCE>`. **Check:** one queued event. **If it fails:** a 401 from hookspool means the
   header differs from the secret file; the person compares the file with the destination's secret. A quarantined
   Save and Test message is expected (SETUP.md, Verify).
5. **Outward, human-performed:** the person adds the destination to a notification policy in Alerts. The agent does
   not change policies.
6. **Outward, rollback:** remove `[bobbins.<INSTANCE>]`, run `hookspool check` and `hookspool reload`, then ask the
   person to delete the destination in Cloudflare.
