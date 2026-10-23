# Trello webhook: agent setup

Same procedure as SETUP.md, as steps. Read the **Needs from the human** list first.

## Needs from the human

- A Trello app or power-up secret (value only the person can copy from Trello). Do not ask them to paste it
  into chat.
- The public HTTPS callback URL they choose, and the board id to watch.
- Approval before the webhook is registered at Trello (step 4 is outward).

## Steps

1. **Goal:** the secret file exists. Write the app secret the person provides to
   `/etc/hookspool/secrets/trello` (mode 0600). Do not print it.
   **Check:** `ls -l /etc/hookspool/secrets/trello` shows mode `-rw-------`.
2. **Goal:** the instance table is in the config. Add:
   ```toml
   [bobbins.trello]
   settings = { callback_url = "<CALLBACK_URL>" }
   secrets.secret = { file = "/etc/hookspool/secrets/trello" }
   outputs = { "*" = "hermes:trello" }
   ```
   **Check:** `hookspool bobbin expand trello` succeeds and shows the secret as a reference.
   **If it fails:** a missing `callback_url` is the known gap in SETUP.md. Stop and report it.
3. **Goal:** the config is valid. Run `hookspool check`.
   **Check:** no error mentions `trello`. **If it fails:** fix the table, do not change the bobbin.
4. **Outward:** register the webhook at Trello with `POST https://api.trello.com/1/webhooks/` using
   `callbackURL=<CALLBACK_URL>`, `idModel=<BOARD_ID>`, and the API key and token the person supplies.
   Ask before running this.
5. **Goal:** Trello's HEAD handshake is answered. Run `curl -I <CALLBACK_URL>`.
   **Check:** HTTP 200, empty body. **If it fails:** check the reverse proxy passes `/hooks/trello`.
6. **Goal:** a real action is accepted. Ask the person to change something on the board, then run
   `hookspool list --source trello`.
   **Check:** a queued event with the board id. **If it fails:** a `unverified` result means the callback
   URL does not match the registered one byte for byte.
7. **Rollback:** remove `[bobbins.trello]`, run `hookspool check` and `hookspool reload`, and delete the
   webhook at Trello (outward, ask first).
