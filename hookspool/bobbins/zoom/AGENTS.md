# Zoom event subscription: agent setup

Same procedure as SETUP.md, as steps. Read the **Needs from the human** list first. The agent never receives
the Secret Token: the person writes the secret file.

## Needs from the human

- The app's Secret Token, written by the person into `/etc/hookspool/secrets/zoom_webhook_secret` (step 1).
  The agent does not read it, print it or repeat it.
- The public HTTPS endpoint URL to use (`<ENDPOINT_URL>`) and the Hermes route for the events (`<ROUTE>`).
- Access to the app's Event Subscriptions page at Zoom, or the person performing step 5.
- Approval before the endpoint is added at Zoom (step 5) and before it is removed (step 7).

Secret used below: `webhook_secret`. There are no required settings.

## Steps

1. **Goal:** the secret file exists. The person writes the Secret Token to
   `/etc/hookspool/secrets/zoom_webhook_secret` with mode 0600. The agent only checks it.
   **Check:** `test -s /etc/hookspool/secrets/zoom_webhook_secret && ls -l /etc/hookspool/secrets/zoom_webhook_secret`
   shows mode `-rw-------`. **If it fails:** ask the person to write the file again.
2. **Goal:** the instance table is in the config. Add:
   ```toml
   [bobbins.zoom]
   secrets.webhook_secret = { file = "/etc/hookspool/secrets/zoom_webhook_secret" }
   outputs = { "*" = "hermes:<ROUTE>" }
   ```
   **Check:** `hookspool bobbin expand zoom` succeeds and shows the secret as a reference only.
   **If it fails:** read the error; it names the missing secret or output.
3. **Goal:** the config is valid and loaded. Run `hookspool check`, then `hookspool reload`.
   **Check:** no error mentions `zoom`. **If it fails:** fix the table, do not change the bobbin.
4. **Goal:** the reverse proxy passes the request on unchanged. Confirm `/hooks/zoom` reaches the intake with
   its body and the `x-zm-request-timestamp` and `x-zm-signature` headers.
   **Check:** `hookspool status` is ok and the proxy config lists the path. **If it fails:** fix the proxy.
5. **Outward, human-performed:** at Zoom, in the app's Event Subscriptions, the person adds a subscription with
   the endpoint URL `<ENDPOINT_URL>`, selects the events and presses Validate. The agent does not do this.
   Ask the person to confirm when Zoom reports the endpoint as validated.
   **If validation fails:** the saved secret differs from the Secret Token on the page; ask the person to save
   the token again (step 1) and press Validate again.
6. **Goal:** a real event is accepted. Ask the person to start and end a test meeting, then run
   `hookspool list --source zoom`.
   **Check:** a queued event whose type is `meeting.started` or `meeting.ended`. **If it fails:** a refused
   delivery usually means a clock more than five minutes off (`timedatectl`) or a proxy that changes the body.
7. **Outward, rollback:** remove `[bobbins.zoom]`, run `hookspool check` and `hookspool reload`. Then ask the
   person to remove or disable the subscription at Zoom. The agent does not do this.
