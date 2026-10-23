# Trello webhook: agent setup

Same procedure as SETUP.md, as steps. Read the **Needs from the human** list first. The agent never
receives the app secret, the Trello API key or the Trello token: the person writes the secret file and
registers the webhook with their own key and token.

## Needs from the human

- The app secret, written by the person into `/etc/hookspool/secrets/trello` (step 1). The agent does not
  read it, print it or repeat it.
- The Trello API key and token the person uses to register the webhook (step 5). The person keeps them;
  they are never given to the agent or put in a file hookspool reads.
- The public HTTPS callback URL to use (`<CALLBACK_URL>`) and the board id to watch (`<BOARD_ID>`).
- Approval before the webhook is registered (step 5) and before it is deleted (step 7).

## Steps

1. **Goal:** the secret file exists. The person writes the app secret to `/etc/hookspool/secrets/trello`
   with mode 0600. The agent only checks it.
   **Check:** `test -s /etc/hookspool/secrets/trello && ls -l /etc/hookspool/secrets/trello` shows mode
   `-rw-------`. **If it fails:** ask the person to write the file again.
2. **Goal:** the instance table is in the config. Add:
   ```toml
   [bobbins.trello]
   settings = { callback_url = "<CALLBACK_URL>" }
   secrets.secret = { file = "/etc/hookspool/secrets/trello" }
   outputs = { "*" = "hermes:trello" }
   ```
   **Check:** `hookspool bobbin expand trello` succeeds and shows the secret as a reference only.
   **If it fails:** a `callback_url` error means the settings line is missing or not a string; fix the table.
3. **Goal:** the config is valid and loaded. Run `hookspool check`, then `hookspool reload`.
   **Check:** no error mentions `trello`. **If it fails:** fix the table, do not change the bobbin.
4. **Goal:** Trello's HEAD handshake is answered before the webhook exists. Run `curl -sI <CALLBACK_URL>`.
   **Check:** the status line is HTTP 200 and there is no body. **If it fails:** check the reverse proxy
   passes `/hooks/trello` unchanged.
5. **Outward, human-performed:** the person registers the webhook with `POST https://api.trello.com/1/webhooks/`
   and the form fields `callbackURL=<CALLBACK_URL>`, `idModel=<BOARD_ID>` and `description`, authenticated with
   the person's own key and token. The agent does not run this request and does not see the key or token.
   Ask the person to confirm when it is done.
6. **Goal:** a real action is accepted. Ask the person to change something on the board, then run
   `hookspool list --source trello`.
   **Check:** one queued event for the board. **If it fails:** an `unverified` result means the callback URL
   does not match the one registered at Trello byte for byte; ask the person to compare the `callbackURL` shown
   on the webhook with the table.
7. **Outward, rollback:** remove `[bobbins.trello]`, run `hookspool check` and `hookspool reload`. Then ask the
   person to delete the webhook at Trello (`DELETE https://api.trello.com/1/webhooks/<WEBHOOK_ID>` with their own
   key and token). The agent does not run the delete.
