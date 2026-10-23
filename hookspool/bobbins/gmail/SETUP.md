# Setting up the gmail bobbin (for a person)

You need: a Google Cloud project with the Gmail API enabled, an OAuth client, and a refresh token with the
`https://www.googleapis.com/auth/gmail.readonly` and `https://www.googleapis.com/auth/calendar.readonly`
scopes for each mailbox (the second is for free/busy). Hookspool must be reachable over HTTPS (see
`deploy/nginx.example.conf`; the query string must reach the intake).

## 1. At Google

1. Create a Pub/Sub topic, for example `gmail-push`, and give `gmail-api-push@system.gserviceaccount.com`  <!-- hookspool-scan: allow: Google's documented Gmail publisher account -->
   the Publisher role on it.
2. Create a **push** subscription on the topic with the endpoint
   `https://<HOST>/hooks/<INSTANCE>?token=<PUSH_TOKEN>`, where `<PUSH_TOKEN>` is a long random string you make
   up (for example `openssl rand -hex 32`) and `<INSTANCE>` is the instance name you choose in step 3.
3. For each mailbox, call `users.watch` with `topicName` set to the topic, `labelIds = ["INBOX"]`. A watch
   lasts seven days.
4. Make an OAuth credential JSON file per mailbox with `client_id`, `client_secret`, `refresh_token` and
   optionally `token_uri`.

## 2. Secrets

Write each value to a file readable only by the hookspool user (mode 0600):

* `/etc/hookspool/secrets/gmail_push_token`: the `<PUSH_TOKEN>`;
* `/etc/hookspool/google/owner.json`: the owner's credential JSON;
* `/etc/hookspool/google/assistant.json`: the assistant's credential JSON (only if there is an assistant
  mailbox).

## 3. Configuration

```toml
[bobbins.gmail]
bobbin = "gmail"
secrets.push_token = { file = "/etc/hookspool/secrets/gmail_push_token" }
secrets.cred_owner = { file = "/etc/hookspool/google/owner.json" }
secrets.cred_agent = { file = "/etc/hookspool/google/assistant.json" }   # leave out with no assistant mailbox
settings = { owner = "owner@example.com", agent = "assistant@example.com", tz = "America/New_York" }
outputs = { scheduling = "hermes:mail-scheduling", agent-mail = "hermes:assistant-mail", agent-schedule = "hermes:assistant-schedule", mailbox-changed = "hermes:mail-changed" }
expect = true
```

Leave out the outputs you do not want. Check it and look at what it expands to:

```
hookspool check
hookspool bobbin expand gmail
```

The state directory (default `<home>/services/gmail`) must be in the dispatcher unit's `ReadWritePaths`
(`deploy/hookspool-dispatch.service`) and private (mode 0700). To keep the history ids of the previous delta
script, set `state_dir` to its data directory.

## 4. Verify

1. `hookspool reload`.
2. Send the owner a test mail asking "are you available for a call next week?" from another address.
3. `hookspool list --source gmail` shows a batch for the mailbox after the `mail` span (30 s quiet); it ends
   `delivered`, and the Hermes route named in `outputs` receives a `scheduling` event. `hookspool status`
   shows the destination `script:gmail` closed.
4. The first push for a mailbox only records the history id; mail that arrived before it is not reported.

## 5. The sweep, and rollback

Pub/Sub retries a failed push for the subscription's retention, so the only gap is a lapsed watch. Run a
daily job that calls `users.watch` again for each mailbox, and set `expect = true` (4 days of silence opens
a `source_silent` issue). To roll back, set the subscription endpoint back to the previous consumer; the spool
keeps what it already accepted. Removing the `[bobbins.gmail]` table stops the intake and the processor; the
state directory is not deleted.
