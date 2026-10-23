# Setting up the drive bobbin (for an agent)

Follow these steps in order. Steps marked `outward` change something at the provider or in a shared account:
ask the person before running them.

## Needs from the human

* A Google Cloud project with the Drive API enabled and permission for you to use it (or the person runs the
  `outward` steps themselves).
* An OAuth credential JSON for the account whose Drive is watched, with `client_id`, `client_secret`,
  `refresh_token` and the scope `drive.metadata.readonly`, saved on this host. The consent and the refresh token
  can only be produced by the account's owner. Never print or paste its contents.
* The public HTTPS host name that reaches hookspool, and the addresses that count as "us" (their own edits are
  not reported).
* Which outputs (`file-changed`, `file-created`, `file-shared`) go to which destinations, which MIME types to
  ignore, and the id of a shared drive if only one is to be watched.

Secrets used below: `channel_token` (made in step 2) and `cred`. There are no required settings.

## Steps

1. **Check the host.** Goal: hookspool is installed and its intake is reachable over HTTPS. Run
   `hookspool status`; expect an ok status. If the intake is unreachable, stop and ask the person.
2. **Make the channel token.** Goal: a random secret exists that the watch channel and hookspool share. Run
   `umask 077; openssl rand -hex 32 > <SECRETS_DIR>/drive_channel_token`. Check mode 0600 and the hookspool user
   as owner (`ls -l`; never print the content). If the mode or owner is wrong, run `chmod 600` and `chown` on
   it; if the file is empty or cannot be written, stop and tell the person.
3. **Place the credential.** Goal: `<CRED_FILE>` is a regular file, mode 0600, owned by the hookspool user,
   with JSON keys `client_id`, `client_secret`, `refresh_token`. Check with
   `python3 -c "import json,sys; print(sorted(json.load(open(sys.argv[1]))))" <CRED_FILE>` (key names only). If the file is
   missing, unreadable by the hookspool user, or lacks a key, stop and ask the person for a correct credential;
   do not try to produce a refresh token yourself.
4. **Add the configuration.** Goal: the instance expands and checks clean. Add:

   ```toml
   [bobbins.drive]
   bobbin = "drive"
   secrets.channel_token = { file = "<SECRETS_DIR>/drive_channel_token" }
   secrets.cred = { file = "<CRED_FILE>" }
   settings = { own = ["<OWNER_ADDRESS>"] }
   outputs = { file-shared = "<DESTINATION>" }
   expect = true
   ```

   Add `skip_mime_types`, `drive_id` and further outputs only if the person asked. Run `hookspool check`
   (expect exit 0) and `hookspool bobbin expand drive` (references only, never values). Fix what check names
   and run it again.
5. **Make the state directory writable.** Goal: the dispatcher unit can write `<STATE_DIR>` (default
   `<HOME>/services/drive`), mode 0700, listed in `ReadWritePaths` of `deploy/hookspool-dispatch.service`. If it
   is not, tell the person: editing the unit is a system change.
6. **Reload.** Goal: the running dispatcher uses the new instance. Run `hookspool reload`; check
   `hookspool status` has no config issue. If it names one, fix the configuration (step 4) or the state
   directory (step 5), run `hookspool check` and reload again.
7. **Create the watch channel** (`outward`). Goal: Google pushes Drive changes to hookspool. Get a start page
   token with `GET https://www.googleapis.com/drive/v3/changes/startPageToken`, then call `changes.watch`
   (`POST https://www.googleapis.com/drive/v3/changes/watch?pageToken=<START_TOKEN>`). If `drive_id` is set
   in the configuration, add `driveId=<DRIVE_ID>&supportsAllDrives=true&includeItemsFromAllDrives=true` to both
   calls; without them the channel watches the account's own feed and changes in the shared drive send no
   push. Call `changes.watch` with `type = web_hook`,
   `address = https://<HOST>/hooks/drive`, a fresh `id`, `token` equal to the content of the token file (read it
   inside the command, do not echo it) and an `expiration` at most a week ahead. Check the response has a
   `resourceId` and an `expiration`, and keep both for step 9. Then check
   `hookspool list --source drive`: it shows no batch yet (Google's `sync` ping is verified and acknowledged
   but never queued, so a missing batch is correct; a `401` line in the intake log means the channel `token`
   differs from the token file). If `changes.watch` answers 4xx, read the error `reason`: Drive API not
   enabled or scope missing (`drive.metadata.readonly`) means the person must fix the Google Cloud project or
   consent; "address not HTTPS" or "not reachable" means the host name or the TLS and proxy setup is wrong
   (check `https://<HOST>/hooks/drive` answers from outside); `channelIdNotUnique` means use a fresh `id`.
   Do not retry in a loop; fix the cause, then repeat this step with a new `id`.
8. **Verify.** Goal: a real push is received and processed. Ask the person to change a file in the watched
   Drive from an account that is not in `own`. Check `hookspool list --source drive` shows a batch that ends
   `delivered`. `retry` means a transient error (`hookspool show <BATCH_ID> --attempts`); `dead` with `exit 65`
   means Google refused the credential or the request: tell the person. Optionally,
   `hookspool source test drive --request <FILE>` checks the request path offline: it queues nothing, and it
   verifies the request against the configured token, so `<FILE>` must be a capture that carries the real
   channel token (the shipped `captures/event.http` carries a fixture token and will report `unverified`). Only
   a real push proves delivery.
9. **Renew the channel daily** (`outward`: installs a recurring job). Goal: the channel never lapses. Schedule a
   job that creates a new channel as in step 7 and stops the old one
   (`POST https://www.googleapis.com/drive/v3/channels/stop` with the old `id` and `resourceId`). Check that
   `hookspool status` shows `last_event_age_s` moving after a real change (a quiet Drive may show a large age
   until something changes; that alone is not a failure). If the new channel call fails (4xx), keep the old
   channel running, fix the cause as in step 7, and do not stop the old channel until a new one exists.

Rollback: stop the channel (`channels.stop`), remove the `[bobbins.drive]` table and run `hookspool reload`.
Nothing in the spool is lost.
