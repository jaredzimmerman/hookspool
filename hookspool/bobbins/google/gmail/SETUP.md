---
bobbin: google.gmail
provider: Google Gmail
webhook_docs: https://developers.google.com/workspace/gmail/api/guides/push
---

# Setting up the google.gmail bobbin (for a person)

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
bobbin = "google.gmail"
secrets.push_token = { file = "/etc/hookspool/secrets/gmail_push_token" }
secrets.cred_owner = { file = "/etc/hookspool/google/owner.json" }
secrets.cred_agent = { file = "/etc/hookspool/google/assistant.json" }   # leave out with no assistant mailbox
outputs = { scheduling = "hermes:mail-scheduling", agent-mail = "hermes:assistant-mail", agent-schedule = "hermes:assistant-schedule", mailbox-changed = "hermes:mail-changed" }
expect = true

[bobbins.gmail.settings]
owner = "owner@example.com"
agent = "assistant@example.com"
tz = "America/New_York"
```

Write `settings` as its own `[bobbins.gmail.settings]` table, after the keys above it. TOML does not let you add to an
inline table (`settings = { ... }`), so with that form the `rule_packs` and `[[bobbins.gmail.settings.rules]]` lines of
the next section are a parse error.

Leave out the outputs you do not want. Check it and look at what it expands to:

```
hookspool check
hookspool bobbin expand gmail
```

The state directory (default `<home>/bobbins/gmail`) must be in the dispatcher unit's `ReadWritePaths`
(`deploy/hookspool-dispatch.service`) and private (mode 0700). To keep the history ids of the previous delta
script, set `state_dir` to its data directory.

### Optional: mail rules

To recognise mail from services that only send email (orders, deliveries, reservations, court alerts), add pack names
or your own rules to the instance's `settings` (for example `rule_packs = ["amazon-orders", "resy-reservations"]` under `[bobbins.gmail.settings]`) and
map the `mail-matched` output in `outputs`. `hookspool bobbin show google.gmail` lists the packs and whether each is `verified` or `unverified`. The rule format,
and how a destination script routes by the item's `rule`, are in [Mail rules and rule packs](../../../../docs/bobbins/mail-rules.md).
A misspelt pack name or a bad rule does not stop the batch: the rules are ignored and the log gets one line, `mail rules ignored for this batch`.
Mail that arrives while the rules are broken is not matched later, even once they are fixed, so run step 4.4 right after every rules edit.

## 4. Verify

1. `hookspool reload`.
2. Send the owner a test mail asking "are you available for a call next week?" from another address.
3. `hookspool list --source gmail` shows a batch for the mailbox after the `mail` span (30 s quiet); it ends
   `delivered`, and the Hermes route named in `outputs` receives a `scheduling` event. `hookspool status`
   shows the destination `script:gmail` closed.
4. With rules on: nobody can send mail from a provider's domain, so a pack cannot be tried that way. Add a temporary
   custom rule of your own, with the address you will send from in `from` and a word of your choice in `subject_any`,
   send the owner a message from that address with that word in the subject, and check that the route mapped to
   `mail-matched` receives one item whose `rule` is the temporary rule's name. Then remove the temporary rule. To see
   that a pack loads, run `hookspool bobbin show google.gmail`, and check the log has no `mail rules ignored` line.
5. The first push for a mailbox only records the history id; mail that arrived before it is not reported.

## 5. The sweep, and rollback

Pub/Sub retries a failed push for the subscription's retention, so the only gap is a lapsed watch. Run a
daily job that calls `users.watch` again for each mailbox, and set `expect = true` (4 days of silence opens
a `source_silent` issue). To roll back, set the subscription endpoint back to the previous consumer; the spool
keeps what it already accepted. Removing the `[bobbins.gmail]` table stops the intake and the processor; the
state directory is not deleted.
