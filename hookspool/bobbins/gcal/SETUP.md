# Setting up the gcal bobbin (for a person)

You need: a Google Cloud project with the Calendar API enabled, an OAuth client, a refresh token for the
calendar's account (scope `https://www.googleapis.com/auth/calendar.readonly`, or
`https://www.googleapis.com/auth/calendar.events` if a travel hook creates blocks), and hookspool reachable
over HTTPS (`deploy/nginx.example.conf`).

## 1. Secrets

Write each value to a file readable only by the hookspool user (mode 0600):

* `/etc/hookspool/secrets/gcal_channel_token`: a long random string you make up (for example
  `openssl rand -hex 32`). It is the `token` of the watch channel in step 3.
* `/etc/hookspool/google/owner.json`: the OAuth credential JSON with `client_id`, `client_secret`,
  `refresh_token` and optionally `token_uri`.

## 2. Configuration

```toml
[bobbins.gcal]
bobbin = "gcal"
secrets.channel_token = { file = "/etc/hookspool/secrets/gcal_channel_token" }
secrets.cred = { file = "/etc/hookspool/google/owner.json" }
settings = { tz = "America/New_York", own = ["owner@example.com", "assistant@example.com"] }
outputs = { conflict = "hermes:calendar-conflict", brief = "hermes:calendar-brief", changed = "script:calendar-refresh" }
expect = true
```

Leave out the outputs you do not want. Add `brief_dir = "/var/lib/briefs"` to `settings` to turn on brief
requests, and `hooks = "/etc/hookspool/gcal_hooks.py"` for travel blocks (README.md). `changed` needs a
`[destinations."script:calendar-refresh"]` of your own. Then:

```
hookspool check
hookspool bobbin expand gcal
```

The state directory (default `<home>/services/gcal`) must be in the dispatcher unit's `ReadWritePaths` and
private (mode 0700). To keep the sync token of the previous delta script, set `state_dir` to its data
directory.

## 3. Create the watch channel

Call `events.watch` on the calendar with `address = https://<HOST>/hooks/gcal`, `type = web_hook`, an `id`
you make up, and `token` equal to the content of `gcal_channel_token`. Google first sends a `sync` ping; the
bobbin acknowledges it without queuing anything. A channel cannot be edited: to change the address, create a
new channel and stop the old one after the first push arrives.

## 4. Verify

1. `hookspool reload`.
2. Change an event in the calendar.
3. `hookspool list --source gcal` shows one batch for the calendar after the `calendar` span (45 s quiet);
   it ends `delivered`. The first push stores the sync token and reports nothing; later changes are reported
   (a conflict, a brief, a `changed` notice).

## 5. Renewal sweep and rollback

A channel lasts at most a week. Run a daily job that creates a new channel (same address and token, new `id`)
and stops the old one, and keep `expect = true` (2 days of silence opens a `source_silent` issue). Google
retries a failed push with backoff for a limited time, so a long outage is reconciled by the next push,
which reads from the stored sync token. To roll back, create a channel on the old address and remove the
`[bobbins.gcal]` table; the spool keeps what it accepted.
