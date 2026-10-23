# Kaneo webhook: agent setup

Same procedure as SETUP.md, as steps. Read the **Needs from the human** list first.

## Needs from the human

- The Kaneo project to connect and the events to send.
- The public HTTPS callback URL to use.
- The secret the person generates. Write it to the file in step 1; do not print it.
- Approval before the integration is created or changed in Kaneo (step 3 is outward).

## Steps

1. **Goal:** the secret file exists. Write the secret to `/etc/hookspool/secrets/kaneo` (mode 0600).
   **Check:** `ls -l /etc/hookspool/secrets/kaneo` shows mode `-rw-------`.
2. **Goal:** the instance table is in the config. Add:
   ```toml
   [bobbins.kaneo]
   secrets.secret = { file = "/etc/hookspool/secrets/kaneo" }
   outputs = { "*" = "hermes:kaneo" }
   ```
   **Check:** `hookspool bobbin expand kaneo` succeeds and shows the secret as a reference only. Then
   `hookspool check` shows no error that mentions `kaneo`.
3. **Outward:** create the generic webhook integration in the Kaneo project with URL `<CALLBACK_URL>` and
   the secret, and choose the events. Ask before running this.
4. **Goal:** a real event is queued. Ask the person to create or change a task, then run
   `hookspool list --source kaneo`.
   **Check:** one queued event with the project id. **If it fails:** `unverified` means the secret differs
   or a proxy changed the body.
5. **Goal:** a tampered body is refused. Run `hookspool source test kaneo --request
   <PATH_TO_bobbins/kaneo/captures/tampered.http>`.
   **Check:** the result is `unverified`.
6. **Rollback:** remove `[bobbins.kaneo]`, run `hookspool check` and `hookspool reload`, and delete the
   integration in Kaneo (outward, ask first).
