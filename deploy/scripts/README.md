# Delta scripts

`gmail_delta.py` and `gcal_delta.py` are the Gmail and Google Calendar "delta" scripts, ported to run as
`script:` destinations of hookspool. They keep their logic and change how they are called and how they
report:

| | before (bridge process) | now (`--batch`) |
|---|---|---|
| input | one push, in-process call | the batch document on stdin (every push for one mailbox or calendar) |
| output | return value, forwarded to the agent by the bridge | `{"enqueue": ...}` lines on stdout, committed with the batch's delivered mark |
| state | files at hard-coded paths, written as the script went | the same files, written only after a journal in `$HOOKSPOOL_STATE_DIR` records the result |
| debounce | in memory, per calendar | the `calendar` / `mail` coalescing span of the source |
| settings | constants in the file | environment (the destination's `env_file`) |

`hookspool_batch.py` is the shared helper (stdlib only): reads the document, builds and prints emits,
stages state writes and keeps the journal. Copy the three files together.

## Why the journal

A delta script advances its own state (a Gmail history id, a Calendar sync token) as it reads. Killed
between "state advanced" and "result committed", a naive script would lose the result: the retry finds
nothing new. So a batch is processed as: compute the result and stage the state writes; write both to
`<state_dir>/journal/<batch id>.json` (fsync, rename); apply the state writes; print the emits. A retry of
the same batch (same `HOOKSPOOL_BATCH_ID`) replays the journal and does not touch Google again. Entries
older than two weeks are removed. Each journal also records a digest of every state file as the pass read it;
a replay that finds a file changed since (a later batch for the same key already moved it on) skips that write
and prints a note on stderr, so replaying an old, never-applied journal cannot move the history id or sync token
backwards and make the next pass report the same changes again. Because the destination is `script`, its delivery id and batch id are
identical on every attempt.

## Destination (placeholders)

```toml
[destinations."script:gcal_delta"]
argv      = ["/usr/bin/python3", "/opt/hookspool/scripts/gcal_delta.py", "--batch"]
cwd       = "/opt/hookspool/scripts"
env_file  = "/etc/hookspool/gcal_delta.env"
state_dir = "/var/lib/hookspool/scripts/gcal"     # journal; also the default data dir; add to ReadWritePaths
timeout_s = 300
max_inflight = 1                                  # required, see below
```

Keep `max_inflight = 1` on both delta destinations (it is the `script` default; do not raise it). The script's
file lock covers reading the sync token or history id and staging the new state, but the staged state is
applied by `hookspool_batch.py` after the lock is released. Two passes in flight on the same state file
(`max_inflight > 1`, two calendar keys or mailboxes sharing one destination, or a manual run during a
delivery) would both read the old token and both report the same changes.

Every directory a script writes (`state_dir`, a `GCAL_DELTA_DATA` / `GMAIL_DELTA_DATA` that differs from
it, the credential file the token refresh rewrites) must be in the dispatcher unit's `ReadWritePaths`.

The env file holds `KEY=value` lines (see the header of each script for every variable):

```
GCAL_DELTA_CRED=/etc/hookspool/google/owner.json
GCAL_DELTA_OWN=owner@example.com,assistant@example.com
GCAL_DELTA_TZ=America/New_York
HOOKSPOOL_DELTA_ROUTES={"conflict":"hermes:calendar-conflict","brief":"hermes:calendar-brief"}
```

`HOOKSPOOL_DELTA_ROUTES` maps the script's event types to destinations. Without it an event type `x` goes to
`hermes:x`. Event types: gcal `conflict`, `brief`; gmail `scheduling`, and for the assistant's own mailbox
`agent-mail` and `agent-schedule`.

## What the emits look like

`{"enqueue": {"source": "gcal", "key": "conflict", "action": "hermes:calendar-conflict", "route": "calendar-conflict",
"payload": {...the original result...}, "event_type": "conflict", "dedup_id": "<delivery id>:<n>", "quiet_s": 0, "max_s": 0}}`

Results are already coalesced by the source, so they are not delayed again. A Hermes route that receives
them gets hookspool's batch document: the original result is `events[0]` (a prompt template that read
`{subject}` now reads the event inside `events`), and a route fed by hookspool must not set `events` or
`coalesce` itself (`hookspool check` refuses it).

## Differences from the production originals

* All deployment values are environment variables; nothing else in the files names an account, path or host.
* `process()` writes state through `hookspool_batch.save`, which stages while a batch runs and writes through otherwise,
  so `python3 gcal_delta.py` with a payload on stdin still works as before.
* The assistant-mailbox scheduling hand-off (a module imported by name in the original) is an optional module named by
  `GMAIL_DELTA_SCHED_MODULE`; its event types are `agent-mail` / `agent-schedule` (the original used other names, so a
  route that filtered on them is mapped through `HOOKSPOOL_DELTA_ROUTES`), and its state file name is
  `GMAIL_DELTA_AGENT_STATE`.
* Travel blocks, the event-store refresh and meeting briefs in `gcal_delta.py` are off unless their variables are set.
* A missing state directory or credential is a clear failure, not a traceback.

`tests/test_delta_scripts.py` runs both scripts against a scripted Google stub, runs the originals (when
`vps-src/` is present) on the same inputs and compares results, state files and API calls, and runs them through the
real `script` destination and dispatcher. `make_diffs.sh <dir with the originals>` writes the diffs against the
originals into the gitignored `deploy/local/scripts/` (they contain the originals' account names, so they are not committed).
