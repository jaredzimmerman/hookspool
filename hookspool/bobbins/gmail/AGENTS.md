# Setting up the gmail bobbin (for an agent)

Follow these steps in order. Steps marked `outward` change something at the provider or in a shared account:
ask the person before running them.

## Needs from the human

* A Google Cloud project with the Gmail API and the Pub/Sub API enabled, and permission for you to use it
  (or the person runs the `outward` steps themselves).
* The owner's mailbox address, and the assistant's mailbox address if there is one.
* An OAuth credential JSON for each mailbox with `client_id`, `client_secret`, `refresh_token` (the consent
  screen and the refresh token can only be produced by the mailbox's owner), saved on this host. Never print
  or paste their contents.
* The public HTTPS host name that reaches hookspool, and the time zone of the owner.
* Which outputs (`scheduling`, `agent-mail`, `agent-schedule`, `mailbox-changed`) go to which Hermes routes.

Secrets used below: `push_token` (made in step 2), `cred_owner`, `cred_agent` (optional). Required setting:
`owner`.

## Steps

1. **Check the host.** Goal: hookspool is installed and its intake is reachable. Run `hookspool status`. Expect an ok status. If the intake is not reachable over HTTPS, stop and ask the
   person: the proxy line must keep the query string (`deploy/nginx.example.conf`).
2. **Make the push token.** Goal: a secret only this subscription knows. Run
   `umask 077; openssl rand -hex 32 > <SECRETS_DIR>/gmail_push_token`. Check the file mode is 0600 and the
   owner is the hookspool user. Do not print it.
3. **Place the credentials.** Goal: `<CRED_OWNER_FILE>` (and `<CRED_AGENT_FILE>`) are regular files, mode
   0600, owned by the hookspool user, holding JSON with `client_id`, `client_secret`, `refresh_token`. Check
   with `python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(sorted(d))" <CRED_OWNER_FILE>`
   (prints key names only). If a key is missing, ask the person.
4. **Add the configuration.** Goal: the instance expands and checks clean. Add to the config file:

   ```toml
   [bobbins.gmail]
   bobbin = "gmail"
   secrets.push_token = { file = "<SECRETS_DIR>/gmail_push_token" }
   secrets.cred_owner = { file = "<CRED_OWNER_FILE>" }
   secrets.cred_agent = { file = "<CRED_AGENT_FILE>" }
   settings = { owner = "<OWNER_ADDRESS>", agent = "<AGENT_ADDRESS>", tz = "<TIME_ZONE>" }
   outputs = { scheduling = "<DESTINATION>" }
   expect = true
   ```

   Leave out `secrets.cred_agent` and `agent` when there is no assistant mailbox. Run `hookspool check`
   (expect exit 0) and `hookspool bobbin expand gmail` (it shows references, never values). If check names a
   missing secret, setting or output, fix the table and run it again.
5. **Make the state directory writable.** Goal: the dispatcher unit can write `<STATE_DIR>` (default
   `<HOME>/services/gmail`), mode 0700. Check `deploy/hookspool-dispatch.service` lists it under
   `ReadWritePaths`; if not, tell the person: editing the unit is a system change.
6. **Reload.** Run `hookspool reload`. Check `hookspool status` has no config issue.
7. **Create the Pub/Sub topic and subscription** (`outward`). Goal: Gmail can publish to the topic and Pub/Sub
   pushes to hookspool. Create topic `<TOPIC>`, grant `gmail-api-push@system.gserviceaccount.com` the  <!-- hookspool-scan: allow: Google's documented Gmail publisher account -->
   Publisher role on it, then create a push subscription with endpoint
   `https://<HOST>/hooks/gmail?token=<PUSH_TOKEN_FROM_FILE>`. Read the token from the file only inside this
   command; do not echo it. Check `gcloud pubsub subscriptions describe <SUBSCRIPTION>` shows the endpoint
   host and path.
8. **Start the watch** (`outward`). Goal: Gmail publishes changes of each mailbox. Call `users.watch` for
   each mailbox with `topicName` `projects/<PROJECT>/topics/<TOPIC>` and `labelIds` `["INBOX"]`. Check the
   response has a `historyId` and an `expiration` seven days away.
9. **Verify.** Goal: a push arrives and is processed. Ask the person to send the owner a mail asking for
   availability, or use `hookspool source test gmail --request <HOOKSPOOL_DIR>/bobbins/gmail/captures/event.http`
   for the request path alone. Check `hookspool list --source gmail` shows a batch that ends `delivered`.
   If it ends `retry`, read the error with `hookspool show <BATCH_ID>`; `dead` with `exit 65` means a
   credential or request was refused: tell the person.
10. **Renew the watch daily** (`outward`: installs a recurring job). Goal: the watch never lapses. Schedule
    a job that repeats step 8 once a day. Check that `hookspool status` shows `last_event_age_s` moving.

Rollback: remove the `[bobbins.gmail]` table, run `hookspool reload`, and set the subscription endpoint
back to the previous consumer. Nothing in the spool is lost.
