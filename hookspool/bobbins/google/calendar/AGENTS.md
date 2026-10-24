---
bobbin: google.calendar
provider: Google Calendar
webhook_docs: https://developers.google.com/workspace/calendar/api/guides/push
---

# Setting up the google.calendar bobbin (for an agent)

Follow these steps in order. Steps marked `outward` change something at the provider or in a shared account:
ask the person before running them.

## Needs from the human

* A Google Cloud project with the Calendar API enabled and permission for you to use it (or the person runs
  the `outward` steps themselves).
* An OAuth credential JSON for the calendar's account with `client_id`, `client_secret`, `refresh_token`,
  saved on this host. The consent and the refresh token can only be produced by the account's owner. Never
  print or paste its contents. The scope must include `calendar.events` if travel blocks are wanted.
* The public HTTPS host name that reaches hookspool, the calendar's time zone, and the addresses that count
  as "us" (the owner, the household, the agents).
* Which outputs (`conflict`, `brief`, `changed`) go to which destinations, and whether briefs
  (`brief_dir`) and travel blocks (a hooks file) are wanted.

Secrets used below: `channel_token` (made in step 2) and `cred`. There are no required settings.

## Steps

1. **Check the host.** Goal: hookspool is installed and its intake is reachable over HTTPS. Run
   `hookspool status`; expect an ok status. If the intake is unreachable, stop and ask the person.
2. **Make the channel token.** Run `umask 077; openssl rand -hex 32 > <SECRETS_DIR>/calendar_channel_token`.
   Check mode 0600 and the hookspool user as owner. Do not print it.
3. **Place the credential.** Goal: `<CRED_FILE>` is a regular file, mode 0600, owned by the hookspool user,
   with JSON keys `client_id`, `client_secret`, `refresh_token`. Check with
   `python3 -c "import json,sys; print(sorted(json.load(open(sys.argv[1]))))" <CRED_FILE>` (key names only).
4. **Add the configuration.** Goal: the instance expands and checks clean. Add:

   ```toml
   [bobbins.calendar]
   bobbin = "google.calendar"
   secrets.channel_token = { file = "<SECRETS_DIR>/calendar_channel_token" }
   secrets.cred = { file = "<CRED_FILE>" }
   settings = { tz = "<TIME_ZONE>", own = ["<OWNER_ADDRESS>"] }
   outputs = { conflict = "<DESTINATION>" }
   expect = true
   ```

   Add `brief_dir`, `hooks` and further outputs only if the person asked. Run `hookspool check` (expect exit
   0) and `hookspool bobbin expand calendar` (references only, never values). Fix what check names and run it
   again.
5. **Make the state directory writable.** Goal: the dispatcher unit can write `<STATE_DIR>` (default
   `<HOME>/bobbins/calendar`), mode 0700, listed in `ReadWritePaths` of `deploy/hookspool-dispatch.service`. If
   it is not, tell the person: editing the unit is a system change.
6. **Reload.** Run `hookspool reload`; check `hookspool status` has no config issue.
7. **Create the watch channel** (`outward`). Goal: Google pushes calendar changes to hookspool. Call
   `events.watch` on `<CALENDAR_ID>` with `type = web_hook`, `address = https://<HOST>/hooks/calendar`, a fresh
   `id` and `token` equal to the content of the token file (read it inside the command, do not echo it). Check
   the response has a `resourceId` and an `expiration` at most a week away. Then check
   `hookspool list --source calendar`: the `sync` ping is acknowledged and not queued.
8. **Verify.** Goal: a real push is received and processed. Ask the person to change an event on
   `<CALENDAR_ID>`. Check `hookspool list --source calendar` shows a batch that ends `delivered`. `retry` means
   a transient error (`hookspool show <BATCH_ID> --attempts`); `dead` with `exit 65` means Google refused the
   credential or the request: tell the person. Optionally, `hookspool source test calendar --request <FILE>`
   checks the request path offline: it queues nothing, and it verifies the request against the configured
   token, so `<FILE>` must be a capture that carries the real channel token (the shipped
   `captures/event.http` carries a fixture token and will report `unverified`). Only a real push proves
   delivery.
9. **Renew the channel daily** (`outward`: installs a recurring job). Goal: the channel never lapses. Schedule
   a job that creates a new channel as in step 7 and stops the old one (`channels.stop`). Check that
   `hookspool status` shows `last_event_age_s` moving.

Rollback: remove the `[bobbins.calendar]` table, run `hookspool reload`, and re-create the channel on the
previous address. Nothing in the spool is lost.
