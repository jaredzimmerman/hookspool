---
bobbin: google.tasks
provider: Google Tasks
api_docs: https://developers.google.com/workspace/tasks/reference/rest
---

# google.tasks

Google Tasks, polled. The Tasks API has no watch or push method, so this is a poll bobbin
([Poll sources](../../../../docs/architecture/ARCHITECTURE.md#144-poll-sources)): the dispatcher's clock enqueues
one tick every `every_s` seconds (300 by default), and `poller.py` asks each followed task list what changed since
the last tick. You get one item per task that was added, edited, completed, reopened or deleted, with its list id,
task id, status and times. The title is included only if you turn `include_titles` on; notes and links are never
read. No model runs here.

```
dispatcher clock -> tick -> poller.py
   tasklists.list           the account's task lists, filtered by the tasklists setting
   tasks.list per list      updatedMin = the newest update seen, minus a minute; completed, hidden, deleted included
   task-changed per task, then one tasks-synced for the pass
```

The Tasks API calls (listing the lists, paging a list's changes, the "list is gone" answer) are
`hookspool.providers.google`'s.

## What a pass does

1. Lists the account's task lists and keeps those named by `tasklists` (an id, or a title ignoring case). With
   the setting empty, every list is followed.
2. A list the state has not seen before is only recorded: its newest update time and the tasks within a minute
   of it are stored and nothing is reported. So the first pass, and any list added later, emits no history. A
   list too big to read in one pass (over 10,000 retained tasks) is recorded as standing at the tick time
   instead, because the API returns pages in no fixed order and a partial read says nothing about where the list
   stands.
3. For a list already recorded, reads the tasks modified since its stored time less a 60-second margin
   (`updatedMin`, with `showCompleted`, `showHidden` and `showDeleted` on, because a task finished in Google's own
   apps is hidden and a deleted one only shows when asked for). The margin picks up a write that was stamped a
   moment before another one but became visible after it. A task already reported with the same update time is
   skipped, so nothing is reported twice; a task edited again is reported again. If the read hits the page cap,
   it reports and remembers the tasks it got, and the stored time moves to the newest of them (or one minute on,
   when every task it got sat inside the margin). The next pass then asks for a later window.
4. Emits one `task-changed` per changed task, oldest first, then one `tasks-synced` if anything changed.

A pass with no changes emits nothing. It costs one call for the list of lists and one per followed list.

## Outputs

| output | when | carries |
|---|---|---|
| `task-changed` | a task was added, edited, completed, reopened or deleted | `tasklist_id`, `task_id`, `status` (`needsAction` or `completed`), `updated`, `due` (empty when none), `deleted` (true when the task was deleted), and `title` only with `include_titles` |
| `tasks-synced` | once per pass that found changes | `tasklists` (the ids of the lists that changed) and `changed_count` |

Every item also carries `source` (`google-tasks`) and `key` (`task:<task id>` for a task, `tasks` for the summary),
which becomes the Hermes event key.

## Secrets

| name | meaning |
|---|---|
| `cred` | OAuth credential JSON (`client_id`, `client_secret`, `refresh_token`, optional `token_uri`). The scope `https://www.googleapis.com/auth/tasks.readonly` is enough. |

The reference must be `file` or `env_file`. The credential file is only read: the poller keeps its access token
and any rotated refresh token in its own runtime state under the state directory (clear text, so a `state_dir`
you set yourself must be private, mode 0700).

## Settings

| setting | type | default | meaning |
|---|---|---|---|
| `tasklists` | list | `[]` | task lists to follow, by id or title; empty follows all of them |
| `include_titles` | bool | `false` | put each task's title in `task-changed` |
| `api_url` | string | Tasks v1 base URL | change only to use a test server |

How often it polls is not a setting but the instance's `every_s` (default 300, at least 60), with an optional
`jitter_s`:

```toml
[bobbins.tasks]
bobbin = "google.tasks"
every_s = 600
```

## State

Under the instance's state directory (default `<home>/bobbins/<instance>/`): `tasks_poll_state.json`, one entry per
followed list `{updated, seen}`, where `seen` maps the tasks updated within the margin to their update time. The runtime writes state only after its journal holds the result: a retried tick
reports the same items and advances the cursor once. To start again from "now", stop the dispatcher's use of the
instance, delete that file and let the next tick record where each list stands.

## Limits

* A change is reported up to `every_s` seconds late, plus the jitter. A webhook would be faster, but Google Tasks
  has no push to offer.
* A pass reads at most 100 pages of 100 tasks from each list, and the pages come in no useful order. When a read
  is cut short, the tasks it got are reported and remembered, so none is reported twice. The stored time then moves
  to the newest of them, or one minute on if they all fell inside the margin. The list never stalls, but a task the
  capped read missed is lost once its update time falls behind the new window. The log shows `capped=N`. Normal use
  is far below the cap.
* A task whose update time is stamped more than 60 seconds before the newest one already seen, and only becomes
  visible after that one was read, is never reported. Two clients writing at nearly the same moment fall well
  inside the margin.
* A list with more than 10,000 retained tasks (completed, hidden and deleted ones pile up) starts from the tick
  time, so a task changed in the minute before the first pass can be reported once. The tick time comes from the
  host's clock, but Google compares `updatedMin` with its own. If the host clock runs more than a minute ahead of
  Google's, writes made in that gap are never reported, so keep the host clock synced.
* A name in `tasklists` that matches no list is ignored; the log line shows `unmatched=N`, so a typo is visible.
* A task list added after the bobbin started is recorded on its first sighting; tasks created in it before that
  moment are not reported.
* Tasks assigned to the account from Docs or Chat Spaces are not returned by the API unless asked for, and are not
  asked for.
* Errors map as everywhere: 408, 425, 429 and 5xx retry the tick (exit 75); another 4xx is permanent (exit 65,
  dead-lettered). A list deleted during a pass is skipped.
* The Tasks API shares a daily quota with the Google Cloud project; at the default interval one list costs about 600
  calls a day.
* Logs carry counts only, never titles.
