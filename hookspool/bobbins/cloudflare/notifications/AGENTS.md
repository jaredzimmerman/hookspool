---
bobbin: cloudflare.notifications
provider: Cloudflare Notifications
webhook_docs: https://developers.cloudflare.com/notifications/get-started/configure-webhooks/
---

# Cloudflare Notifications webhook: agent setup

Same procedure as SETUP.md, as steps. Read the **Needs from the human** list first. Repeat steps 1 to 8 for each
instance. The agent never receives the webhook secret: the person writes the secret file and creates the
destination in Cloudflare.

## Needs from the human

- The instance name (`<INSTANCE>`) and the public HTTPS callback URL for it (`<CALLBACK_URL>`).
- The webhook secret, written by the person into `/etc/hookspool/secrets/<INSTANCE>` with mode 0600 (step 1).
  The agent does not read it, print it or repeat it.
- Whether Cloudflare retries a failed delivery. It is not documented; the person confirms it with Cloudflare (step 5).
- Approval before the proxy check sends a request to the public callback (step 3), before the destination is created
  in Cloudflare (step 4) and before a notification policy uses it (step 6).

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
3. **Goal:** the public path `/hooks/<INSTANCE>` reaches hookspool with the path unchanged and no query string.
   Add the proxy location for it, as SETUP.md ("Reverse proxy") describes, then reload the proxy. **Outward check:**
   the check below sends one request to the public callback, so ask the person to confirm before running it. **Check:**
   `curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' --data '{}' <CALLBACK_URL>`
   prints `401`, and `hookspool list --source <INSTANCE>` shows no event for it. **If it fails:** a `404` means the
   proxy does not pass the path through; a `502` or a timeout means hookspool is not reachable from the proxy. Fix
   the proxy and check again. Do not change the bobbin.
4. **Outward, human-performed:** the person creates the webhook destination in the Cloudflare dashboard, under
   Alerts, then Destinations, in the Webhooks card, with the URL `<CALLBACK_URL>` and the secret from step 1.
   The agent does not run this. Ask the person to confirm when it is done.
5. **Goal:** Cloudflare's retry behaviour is known. Ask the person to confirm with Cloudflare whether it retries a
   failed generic webhook delivery, or to test it with a deliberate failure, and record the answer in the instance's
   notes. **Check:** the person has given a yes or no. **If it does not retry:** stop before go-live and ask the
   operator for a sweep for Cloudflare Notifications; SETUP.md ("Retries") explains why.
6. **Outward, human-performed:** the person adds the destination to a notification policy in Alerts. The agent does
   not change policies.
7. **Goal:** an alert is queued, now that a policy uses the destination. Ask the person to wait for an alert, or to
   trigger one the policy sends, then run `hookspool list --source <INSTANCE>`. **Check:** one queued event. **If it fails:** a 401 from hookspool means the
   header differs from the secret file; the person compares the file with the destination's secret. The quarantine
   that Save and Test causes is expected (SETUP.md, Verify).
8. **Outward, rollback:** remove `[bobbins.<INSTANCE>]`, run `hookspool check` and `hookspool reload`, then ask the
   person to delete the destination in Cloudflare.
