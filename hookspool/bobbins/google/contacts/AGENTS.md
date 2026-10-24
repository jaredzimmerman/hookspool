---
bobbin: google.contacts
provider: Google Contacts
api_docs: https://developers.google.com/people/api/rest/v1/people.connections/list
---

# Setting up the google.contacts bobbin (for an agent)

Follow these steps in order. This bobbin polls, so no step creates anything at Google or changes a shared account.
Steps 3 and 4 touch the host's config and state directory; ask first if the person did not request that.

## Needs from the human

* A Google Cloud project with the People API enabled, and an OAuth credential JSON for the account whose contacts
  are followed, with `client_id`, `client_secret`, `refresh_token` and the scope
  `https://www.googleapis.com/auth/contacts.readonly` (also `https://www.googleapis.com/auth/contacts.other.readonly`
  when "Other contacts" are wanted), saved on this host. The consent and the refresh token can only be produced by
  the account's owner. Never print or paste its contents.
* The destination for the `contacts-changed` output, and whether "Other contacts" are wanted.

The secret used below is `cred`. There are no required settings.

## Steps

1. **Check the host.** Goal: hookspool is installed and its dispatcher runs. Run `hookspool status`; expect an ok
   status. If the dispatcher is not running, stop and ask the person.
2. **Place the credential.** Goal: `<CRED_FILE>` is a regular file, mode 0600, owned by the hookspool user, with JSON
   keys `client_id`, `client_secret`, `refresh_token`. Check as that user, since a
   0600 file is not readable by anyone else:
   `sudo -u <HOOKSPOOL_USER> python3 -c "import json,sys; print(sorted(json.load(open(sys.argv[1]))))" <CRED_FILE>`
   (key names only). If the
   file is missing, unreadable by the hookspool user, or lacks a key, stop and ask the person for a correct
   credential; do not try to produce a refresh token yourself.
3. **Add the configuration.** Goal: the instance expands and checks clean. Add:

   ```toml
   [bobbins.contacts]
   bobbin = "google.contacts"
   secrets.cred = { file = "<CRED_FILE>" }
   outputs = { contacts-changed = "<DESTINATION>" }
   ```

   Add `settings = { include_other_contacts = true }` only if the person asked, and `every_s = <SECONDS>` (at
   least 60) only to change the 900-second interval. Run `hookspool check` (expect exit 0) and
   `hookspool bobbin expand contacts` (references only, never values). Fix what check names and run it again.
4. **Make the state directory writable.** Goal: the dispatcher unit can write `<STATE_DIR>` (default
   `<HOME>/bobbins/contacts`), mode 0700, listed in `ReadWritePaths` of `deploy/hookspool-dispatch.service`. If it is
   not, tell the person: editing the unit is a system change.
5. **Reload.** Goal: the running dispatcher uses the new instance. Run `hookspool reload`; check `hookspool status`
   has no config issue and shows `polls.contacts` with a `next_at`. If it names an issue, fix the configuration
   (step 3) or the state directory (step 4), run `hookspool check` and reload again.
6. **Verify.** Goal: a tick is delivered. After `next_at`, check `hookspool list --source contacts` shows a batch
   that ends `delivered`. The first tick stores the sync token and emits nothing, so no item reaches the destination
   yet. `retry` means a transient error (`hookspool show <BATCH_ID> --attempts`); `dead` with `exit 65` means Google
   refused the credential or the request (a missing scope or a disabled People API): tell the person. To see a
   report, ask the person to change a contact, then check the next ticks: one `contacts-changed` item with resource
   names and counts, no personal data (past 2000 names it has `truncated` true and the count only; tell the person
   their job must then reread the address book). A `full_resync` item on every tick means a 400 that is not an
   expired token: tell the person. Google can take a few minutes to show a write to an incremental sync.

Rollback: remove the `[bobbins.contacts]` table and run `hookspool reload`. Nothing in the spool is lost.
