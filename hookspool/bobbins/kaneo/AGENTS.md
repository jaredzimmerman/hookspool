# Kaneo webhook: agent setup

Same procedure as SETUP.md, as steps. Read the **Needs from the human** list first. The agent never receives
the webhook secret: the person writes the secret file and creates the integration in Kaneo.

## Needs from the human

- The Kaneo project to connect (`<PROJECT_ID>`) and the events to send.
- The public HTTPS callback URL to use (`<CALLBACK_URL>`).
- The secret, written by the person into `/etc/hookspool/secrets/kaneo` with mode 0600 (step 1). The agent does
  not read it, print it or repeat it.
- Whether Kaneo retries a failed delivery. It is not documented; the person confirms it with Kaneo (step 6).
- Approval before the integration is created in Kaneo (step 3) and before it is deleted (step 7).

## Steps

1. **Goal:** the secret file exists. The person writes the secret to `/etc/hookspool/secrets/kaneo`.
   **Check:** `test -s /etc/hookspool/secrets/kaneo && ls -l /etc/hookspool/secrets/kaneo` shows mode `-rw-------`.
   **If it fails:** ask the person to write the file again.
2. **Goal:** the instance table is in the config, loaded and valid. Add:
   ```toml
   [bobbins.kaneo]
   secrets.secret = { file = "/etc/hookspool/secrets/kaneo" }
   outputs = { "*" = "hermes:kaneo" }
   ```
   Then run `hookspool bobbin expand kaneo` (it must succeed and show the secret as a reference only),
   `hookspool check`, and `hookspool reload`. **Check:** no error mentions `kaneo`. **If it fails:** fix the table,
   do not change the bobbin.
3. **Outward, human-performed:** the person adds the generic webhook integration in the Kaneo project `<PROJECT_ID>`
   with the URL `<CALLBACK_URL>`, the secret from step 1, and the events chosen. The agent does not run this. Ask the
   person to confirm when it is done.
4. **Goal:** a real event is queued. Ask the person to create or change a task, then run
   `hookspool list --source kaneo`. **Check:** one queued event with the project id. **If it fails:** `unverified`
   means the secret differs or a proxy changed the body.
5. **Goal:** a request with a wrong signature is refused (`outward`: it sends one request over the public URL;
   ask the person first). Run:
   `curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' -H 'X-Kaneo-Signature: 0000' --data '{"event":"task.created"}' <CALLBACK_URL>`
   **Check:** it prints `401`, and `hookspool list --source kaneo` shows no event for it. **If it does not:** the
   request is reaching something other than this instance; check the reverse proxy passes `/hooks/kaneo`.
6. **Goal:** Kaneo's retry behaviour is known. Ask the person to confirm with Kaneo whether it retries a failed
   delivery, and record the answer in the instance's notes. **If it does not retry:** stop before go-live and ask the
   operator for a sweep for Kaneo; the bobbin's SETUP.md ("Retries") explains why.
7. **Outward, rollback:** remove `[bobbins.kaneo]`, run `hookspool check` and `hookspool reload`, then ask the person
   to delete the integration in Kaneo.
