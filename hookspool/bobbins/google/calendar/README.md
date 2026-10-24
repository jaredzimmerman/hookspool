---
bobbin: google.calendar
provider: Google Calendar
webhook_docs: https://developers.google.com/workspace/calendar/api/guides/push
---

# google.calendar

Google Calendar push (a watch channel): a push says that a calendar changed, the processor reads what
changed from Calendar's incremental sync, does the routine work, and wakes the agent only for a judgement
call. No model runs here.

```
Calendar events.watch -> POST /hooks/<instance> (empty body, X-Goog-* headers)
   source.py     verifies the channel token, builds the payload from the headers, keys it by calendar
   processor.py  one pass per batch: changes since the stored sync token -> conflicts, travel, briefs -> items
```

The channel rules (token check, `sync` handshake, header payload, dedup id) are
`hookspool.providers.google`'s and are shared with the google.drive bobbin.

## What a pass does

1. Reads only the events changed since the last pass and drops noise: past events, edits to reminders,
   colour or description only, Google's own birthday, working-location, out-of-office and focus-time entries,
   and the travel blocks this bobbin creates (tracked, never a trigger). "Real" changes are a new event, a
   moved one (start or end), a cancelled one, or an update to the title, location, attendee count, status or
   video link, inside the window from two hours ago to 14 days ahead.
2. Conflicts: a changed event that overlaps another timed event on the calendar (travel blocks and all-day
   events never conflict).
3. Travel (optional, `hooks.travel`): see below. A moved or cancelled event takes the travel blocks this
   bobbin made for it ("Travel -> <title>") with it.
4. Briefs (optional, `brief_dir` set): a new or moved meeting with outside people, three or more
   attendees, or a prep-style title (review, interview, demo, ...) becomes a `brief` request once; a request
   whose brief file never appeared is asked again after six hours, up to four attempts.
5. Reports.

The first push, a `sync` ping, or a rejected sync token (HTTP 410) only re-reads the calendar and stores a
fresh sync token; nothing is reported.

## Outputs

| output | when | carries |
|---|---|---|
| `conflict` | a conflict or "not enough time to get there" needs a choice | `resource_state`, `count`, `dates_text`, `summary` (one line per change), `conflicts_text`, `actions_text` (travel blocks created or removed, hook notes; "nothing" if none) |
| `brief` | a meeting needs a brief | `title`, `when`, `duration_min`, `organizer`, `attendees_text`, `description`, `location`, `link`, `brief_path` (`<brief_dir>/<date>-<slug>-<id>.md`), `event_id` |
| `changed` | once per pass that saw real changes | `count`, `kinds` (`new`, `moved`, `updated`, `cancelled` per change), `event_ids`, `dates` (`YYYY-MM-DD`), `resource_state` |

`changed` replaces the old script's fire-and-forget start of a local refresh script. Map it to a
`script:` destination of your own (it receives the item as its event payload) and the follow-up is retried
like any other delivery; leave it out and it is dropped. Items carry a `key` (`conflict`, `changed`,
`brief:<event id>`) that becomes the Hermes event key.

## Secrets

| name | meaning |
|---|---|
| `channel_token` | the token given when the watch channel was created, echoed by Google in `X-Goog-Channel-Token`. Needed by the source. |
| `cred` | OAuth credential JSON (`client_id`, `client_secret`, `refresh_token`, optional `token_uri`) for the calendar. Needed by the processor. The scope must include event write (`https://www.googleapis.com/auth/calendar.events`) if `hooks.travel` creates blocks, else read-only is enough. |

References must be `file` or `env_file`. The credential file is only read: the processor keeps its access
token and any rotated refresh token in its own runtime state under the state directory (clear text, so a
`state_dir` you set yourself must be private, mode 0700).

## Settings

| setting | type | default | meaning |
|---|---|---|---|
| `tz` | string | `UTC` | time zone for event times and report text |
| `own` | list | `[]` | addresses that are "us" (the owner, the household, the agents): not external attendees |
| `brief_dir` | path | `""` | where meeting briefs are to be written; empty turns brief requests off |
| `api_url` | string | the primary calendar's events URL | use another calendar's events URL, or a test server |
| `hooks` | path | `""` | absolute path of a hooks module, see below |

## Hooks

```python
def travel(event: dict, day_events: dict, ctx) -> dict | None:
    """Called for each changed event that is new, moved or updated, has a real address (a location that is
    not a placeholder such as 'online' or 'zoom') and is not all day."""
```

`event` is the event's signature plus its id: `{"id", "start", "end" (ISO, in the `tz` zone), "summary",
"location", "status", "attendees" (count), "conf" (has a video link), "all_day", "link", "organizer",
"external" (outside addresses), "desc"}`. `day_events` is `{event id: signature}` for every event the
calendar tracks on the same date, including the event itself and existing travel blocks (the primary
calendar's view, so a hook can tell its own events from someone else's). `ctx` is the processor context
(`ctx.http`, `ctx.settings`, ...). Return `None` for nothing to do, or a dict with any of:

* `"block"`: `{"minutes": N, "from": text, "computed_minutes": N, "buffer_minutes": N, "mode": "driving"
  | "transit" | "walking" | "bicycling"}`. The bobbin inserts a transparent block ending when the event
  starts, `N` minutes long, titled "<mode symbol> Travel -> <title>" with a description naming the origin and
  the minutes; it does nothing if a block for that title already exists that day.
* `"conflict"`: text for the `conflict` item (for example "not enough time to get from A to B: needs 35 min,
  gap is 10 min").
* `"note"`: text added to `actions_text` (for example "the routing service is down, no block created").

A hook that raises does not stop the pass: the report says the travel check failed for that date. There is
no subprocess in the bobbin: whatever computes travel time runs inside the hook (through `ctx.http`) or
elsewhere on its own schedule.

## State

Under the instance's state directory (default `<home>/bobbins/<instance>/`): `gcal_push_state.json`
`{syncToken, last_seen, bootstrapped}`, and with briefs on, `briefs_requested.json` (written here) and
`briefs_published.json` (written by whatever publishes the briefs; this bobbin only reads it). The runtime
writes state only after its journal holds the result: a retried batch reports the same items and advances the
sync token once. The processor writes no log or action file of its own: it logs counts to its standard error,
which the dispatcher keeps with the attempt.

## Limits

* A pass lists at most 40 pages of 250 events.
* A channel expires after at most a week; Google does not warn. Renew it daily (SETUP.md) and set
  `expect = true` so silence opens an issue.
* Errors map as everywhere: 408, 425, 429 and 5xx retry the batch (exit 75); another 4xx is permanent
  (exit 65, dead-lettered).
* Travel blocks are the exception. An error while creating one, a 429 or 503 included, goes into `actions_text`
  and the log; one while removing a stale block goes into the log only. Either way the pass still stores the new
  sync token, so that block is not tried again until its event changes. Retrying it safely needs a stable event
  id for the block, which is planned.
* Logs carry counts only, never titles or addresses.

## Moving from 0.1

This bobbin replaces the 0.1 delta script. [Upgrading to 0.2](../../../docs/operations/upgrading.md#upgrading-to-02-bobbins)
maps each of its variables to a setting and keeps the sync token by pointing `state_dir` at the old directory.
