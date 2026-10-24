---
bobbin: google.tasks
provider: Google Tasks
api_docs: https://developers.google.com/workspace/tasks/reference/rest
---

# Setting up the google.tasks bobbin (for a person)

Google Tasks cannot push, so there is nothing to register at Google and no public URL to expose. hookspool asks for
changes on a timer. You need: a Google Cloud project with the Google Tasks API enabled, an OAuth client, and a
refresh token for the account whose tasks you want to follow (scope
`https://www.googleapis.com/auth/tasks.readonly`).

## 1. Secrets

Write the credential to a file readable only by the hookspool user (mode 0600):

* `/etc/hookspool/google/owner.json`: the OAuth credential JSON with `client_id`, `client_secret`,
  `refresh_token` and optionally `token_uri`.

## 2. Configuration

```toml
[bobbins.tasks]
bobbin = "google.tasks"
secrets.cred = { file = "/etc/hookspool/google/owner.json" }
settings = { tasklists = [], include_titles = false }
outputs = { task-changed = "hermes:tasks-changed", tasks-synced = "hermes:tasks-synced" }
```

Leave out the outputs you do not want: their items are dropped. Put the ids or titles of the lists to follow in
`tasklists`; an empty list follows every list of the account. Add `every_s = 600` to the table to poll every ten
minutes instead of every five (not less than 60). Then:

```
hookspool check
hookspool bobbin expand tasks
```

The state directory (default `<home>/bobbins/tasks`) must be in the dispatcher unit's `ReadWritePaths` and private
(mode 0700).

## 3. Verify

1. `hookspool reload`.
2. `hookspool status` shows `polls.tasks` with a `next_at`. The first tick comes within the jitter (about 30
   seconds with the default interval) and only records where each list stands, so it reports nothing.
3. Add, finish or delete a task. Within one interval `hookspool list --source tasks` shows a batch that ends
   `delivered`, and the destination receives a `task-changed` item (and a `tasks-synced`).

## 4. Rollback

Remove the `[bobbins.tasks]` table and run `hookspool reload`. Nothing at Google needs undoing. The spool keeps what
it accepted. If you add the bobbin again later, delete `tasks_poll_state.json` first so that the first tick starts
from "now" instead of reporting everything that changed while it was off.
