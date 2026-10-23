# Setting up the drive bobbin (for a person)

You need: a Google Cloud project with the Drive API enabled, an OAuth client, a refresh token for the account
whose Drive you want to watch (scope `https://www.googleapis.com/auth/drive.metadata.readonly`), and hookspool
reachable over HTTPS (`deploy/nginx.example.conf`).

## 1. Secrets

Write each value to a file readable only by the hookspool user (mode 0600):

* `/etc/hookspool/secrets/drive_channel_token`: a long random string you make up (for example
  `openssl rand -hex 32`). It is the `token` of the watch channel in step 3.
* `/etc/hookspool/google/owner.json`: the OAuth credential JSON with `client_id`, `client_secret`,
  `refresh_token` and optionally `token_uri`.

## 2. Configuration

```toml
[bobbins.drive]
bobbin = "drive"
secrets.channel_token = { file = "/etc/hookspool/secrets/drive_channel_token" }
secrets.cred = { file = "/etc/hookspool/google/owner.json" }
settings = { own = ["owner@example.com"], skip_mime_types = ["application/vnd.google-apps.folder"] }
outputs = { file-changed = "hermes:drive-changed", file-created = "hermes:drive-created", file-shared = "hermes:drive-shared" }
expect = true
```

Leave out the outputs you do not want: their items are dropped. Add `drive_id = "<shared drive id>"` to `settings` to
watch one shared drive instead of the account's own feed. Then:

```
hookspool check
hookspool bobbin expand drive
```

The state directory (default `<home>/services/drive`) must be in the dispatcher unit's `ReadWritePaths` and
private (mode 0700).

## 3. Create the watch channel

First get a start page token (`GET https://www.googleapis.com/drive/v3/changes/startPageToken`). If you set
`drive_id` (a shared drive), add `driveId=<DRIVE_ID>&supportsAllDrives=true&includeItemsFromAllDrives=true` to
that call and to the watch call below; without them the channel watches the account's own feed and changes in the
shared drive send no push. Then call `changes.watch`:
`POST https://www.googleapis.com/drive/v3/changes/watch?pageToken=<that token>` (plus the same three parameters
for a shared drive) with a JSON body
`{"id": "<a channel id you make up>", "type": "web_hook", "address": "https://<HOST>/hooks/drive", "token": "<the content of drive_channel_token>", "expiration": <milliseconds since the epoch, at most a week ahead>}`.
The response carries `resourceId` and `expiration`; keep both, they are needed to stop the channel. Google first
sends a `sync` ping; the bobbin acknowledges it without queuing anything. A channel cannot be edited: to change
the address, create a new channel and stop the old one after the first push arrives.

## 4. Verify

1. `hookspool reload`.
2. Edit a file in the watched Drive (as someone who is not in `own`, or temporarily without `own`).
3. `hookspool list --source drive` shows one batch after the `calendar` span (45 s quiet); it ends `delivered`.
   The first push stores the page token and reports nothing; later changes are reported as `file-changed`,
   `file-created` or `file-shared` items.

## 5. Renewal sweep and rollback

A channel lasts at most a week. Run a daily job that creates a new channel (step 3: same address and token, new
`id`) and stops the old one (`POST https://www.googleapis.com/drive/v3/channels/stop` with
`{"id": "<old id>", "resourceId": "<old resourceId>"}`), and keep `expect = true` (a week without any push opens a
`source_silent` issue; a very quiet Drive can be silent that long, in which case leave `expect` out or raise
`expect_every_s`). A pass reads at most 50 pages of 1000 changes; if a burst is larger, the rest waits for the
next push, which on a quiet Drive can be days. Google retries a failed push with backoff for a limited time, so a long outage is
reconciled by the next push, which reads from the stored page token. To roll back, stop the channel and remove the
`[bobbins.drive]` table; the spool keeps what it accepted.
