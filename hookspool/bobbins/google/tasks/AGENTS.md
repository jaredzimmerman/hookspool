---
bobbin: google.tasks
provider: Google Tasks
api_docs: https://developers.google.com/workspace/tasks/reference/rest
---

# Setting up the google.tasks bobbin (for an agent)

Follow these steps in order. Google Tasks has no push, so no step changes anything at Google: the bobbin only reads
with a credential the person provides.

## Needs from the human

* A Google Cloud project with the Google Tasks API enabled.
* An OAuth credential JSON for the account whose tasks are followed, with `client_id`, `client_secret`,
  `refresh_token` and the scope `tasks.readonly`, saved on this host. The consent and the refresh token can only be
  produced by the account's owner. Never print or paste its contents.
* Which task lists to follow (ids or titles; none means all), whether task titles may leave Google
  (`include_titles`, off by default), and how often to poll (default every 300 seconds).
* Which outputs (`task-changed`, `tasks-synced`) go to which destinations.

Secret used below: `cred`. There are no required settings.

## Steps

1. **Check the host.** Goal: hookspool is installed and the dispatcher is running. Run `hookspool status`; expect an
   ok status. If it is not, stop and ask the person.
2. **Place the credential.** Goal: `<CRED_FILE>` is a regular file, mode 0600, owned by the hookspool user, with
   JSON keys `client_id`, `client_secret`, `refresh_token`. Check with
   `python3 -c "import json,sys; print(sorted(json.load(open(sys.argv[1]))))" <CRED_FILE>` (key names only). If the
   file is missing, unreadable by the hookspool user, or lacks a key, stop and ask the person for a correct
   credential; do not try to produce a refresh token yourself.
3. **Add the configuration.** Goal: the instance expands and checks clean. Add:

   ```toml
   [bobbins.tasks]
   bobbin = "google.tasks"
   secrets.cred = { file = "<CRED_FILE>" }
   settings = { tasklists = [], include_titles = false }
   outputs = { task-changed = "<DESTINATION>" }
   ```

   Add `every_s`, further outputs and the lists to follow only if the person asked. Run `hookspool check` (expect
   exit 0) and `hookspool bobbin expand tasks` (references only, never values). Fix what check names and run it again.
4. **Make the state directory writable.** Goal: the dispatcher unit can write `<STATE_DIR>` (default
   `<HOME>/bobbins/tasks`), mode 0700, listed in `ReadWritePaths` of `deploy/hookspool-dispatch.service`. If it is
   not, tell the person: editing the unit is a system change.
5. **Reload.** Goal: the running dispatcher uses the new instance. Run `hookspool reload`; check `hookspool status`
   has no config issue and shows `polls.tasks` with a `next_at`. If it names an issue, fix the configuration (step 3)
   or the state directory (step 4), run `hookspool check` and reload again.
6. **Verify.** Goal: a real poll runs and the cursor is recorded. Wait for the first tick (within the interval plus
   jitter); check `hookspool list --source tasks` shows a batch that ends `delivered` and that
   `<STATE_DIR>/tasks_poll_state.json` exists. The first tick reports nothing by design. Then ask the person to add
   or finish a task, and check the next tick delivers a `task-changed` batch. `retry` means a transient error
   (`hookspool show <BATCH_ID> --attempts`); `dead` with `exit 65` means Google refused the credential or the
   request (Tasks API not enabled, or the scope is missing): tell the person.

Rollback: remove the `[bobbins.tasks]` table and run `hookspool reload`. Nothing at Google needs undoing.
