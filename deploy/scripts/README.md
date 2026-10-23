# Batch helper for scripts

`hookspool_batch.py` is for a `script:` destination you write yourself that keeps state while it reads an
outside system and reports through emits. It re-exports `hookspool.processor` (the processor runtime of
bobbins, ARCHITECTURE 14.3.4), which reads the batch document, builds and prints emits, stages state writes and
keeps the journal. `import hookspool_batch` gives that very module, so the script needs hookspool installed for
the interpreter that runs it (or run it from this checkout).

The Gmail and Google Calendar "delta" scripts that used to live here are the `gmail` and `gcal` bobbins now
(`hookspool/bobbins/gmail/` and `hookspool/bobbins/gcal/`): their settings replace the old environment variables
(each README lists the mapping), their credentials are secrets, their state lives in
`<home>/services/<instance>` (set `state_dir` to the old directory to keep a history id or sync token), and
what the scripts started on the side (the event-store refresh) is the gcal `changed` output, which a deployment
maps to a `script:` destination of its own.

## Why the journal

A script that advances its own state (a history id, a sync token) as it reads would, if killed between "state
advanced" and "result committed", lose the result: the retry finds nothing new. So a batch is processed as:
compute the result and stage the state writes; write both to `<state_dir>/journal/<batch id>.json` (fsync,
rename); apply the state writes; print the emits. A retry of the same batch (same `HOOKSPOOL_BATCH_ID`)
replays the journal and does not touch the outside system again. Entries older than two weeks are removed.
Each journal also records a digest of every state file as the pass read it; a replay that finds a file changed
since (a later batch for the same key already moved it on) skips that write and prints a note on stderr, so
replaying an old, never-applied journal cannot move a cursor backwards and make the next pass report the same
changes again. Because the destination is `script`, its delivery id and batch id are identical on every
attempt.

## A destination for such a script (placeholders)

```toml
[destinations."script:calendar-refresh"]
argv      = ["/opt/hookspool/venv/bin/python", "/opt/hookspool/scripts/refresh_events.py"]
cwd       = "/opt/hookspool/scripts"
env_file  = "/etc/hookspool/refresh.env"
state_dir = "/var/lib/hookspool/scripts/refresh"   # journal; below the home, so the unit already allows it
timeout_s = 300
max_inflight = 1                                    # the default for script; do not raise it
```

Keep `max_inflight = 1`: the lock covers reading the cursor and staging the new state, but the staged state is
applied after the lock is released, so two passes in flight on one state file would both report the same
changes. A directory outside the home goes in the dispatcher unit's `ReadWritePaths`.

The script's exit code decides the retry: 0 is done; 65 (`hookspool.processor.Permanent`) is dead-lettered at
once; anything else, 75 (`Retryable`) included, is retried with backoff.

## What the emits look like

`{"enqueue": {"source": "refresh", "key": "changed", "action": "hermes:calendar", "route": "calendar",
"payload": {...the original result...}, "event_type": "changed", "dedup_id": "<delivery id>:<n>", "quiet_s": 0,
"max_s": 0}}`

Results are already coalesced by the source, so they are not delayed again. A Hermes route that receives them
gets hookspool's batch document: the original result is `events[0]`, and a route fed by hookspool must not set
`events` or `coalesce` itself (`hookspool check` refuses it).

`tests/test_batch_helpers.py` tests the helper; `tests/bobbins/` tests the two bobbins against a scripted
Google, including through the real `script` destination and dispatcher.
