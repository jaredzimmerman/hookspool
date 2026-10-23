# Command line reference

```
hookspool [--home DIR] [--config FILE] [--json] <command> [options]
```

Every command is a subcommand of one program, and every service is the same program with a different command. The CLI
is checked against this page by `tests/test_docs.py`: a command or option that the code has and this page lacks, or the
other way round, fails the suite.

## Global options and environment

| option | meaning |
|---|---|
| `--home DIR` | state directory. Default: `$HOOKSPOOL_HOME`, then `[spool] home` in the config, then `/var/lib/hookspool` |
| `--config FILE` | the config file. Default: `$HOOKSPOOL_CONFIG`, then `<home>/config.toml` |
| `--json` | machine-readable output for the commands that print a table or a sentence |

| variable | meaning |
|---|---|
| `HOOKSPOOL_HOME` | state directory (below `--home`, above the config and the default) |
| `HOOKSPOOL_CONFIG` | path of the config file (below `--config`) |
| `HOOKSPOOL_DB` | path of the spool file, overriding `[spool] db` |
| `HOOKSPOOL_LOG` | `json` for one JSON object per log line; anything else is the text form |
| `HOOKSPOOL_ALERT_DRY` | set to anything but `0` to record alerts instead of running `alerts.argv` (tests, rehearsals) |
| `HOOKSPOOL_BOBBIN`, `HOOKSPOOL_BOBBIN_INSTANCE`, `HOOKSPOOL_BOBBIN_DIR` | set by the dispatcher for a bobbin's processor (`python -m hookspool.processor <instance>`, ARCHITECTURE 14.3.4): the bobbin, the instance and the bobbin directory. Never set by hand, and never read from the dispatcher's own environment |
| `HOOKSPOOL_SETTINGS`, `HOOKSPOOL_SECRET_REFS`, `HOOKSPOOL_DELTA_ROUTES` | also set for a processor: its typed settings, the references (never values) of the secrets it may read, and the destinations of its event types (its output map), each as JSON |

Precedence for paths is always: option, then environment, then config file, then default.

A missing config file means the defaults (no sources). A config file that exists but does not parse is an error: running
on defaults would hide a broken deploy.

## Exit codes

| code | meaning |
|---|---|
| 0 | done |
| 1 | not found, or nothing to do (an empty `list`, `replay` of a batch that is not dead) |
| 2 | configuration or schema error (also: the intake could not bind its port) |
| 3 | `dispatch.lock` is held: a dispatcher is already running |

## Services

### hookspool intake

Runs the HTTP intake: a server bound to the loopback address in `[intake] listen`, serving `/hooks/<source>`,
`/healthz` and `/metrics`. It never applies schema migrations; on a spool older than the code it exits 2 and says to run
`hookspool migrate`. It reloads the config when the file's modification time changes (checked every 5 seconds).

### hookspool dispatch

Runs the dispatcher, the only process that delivers. It takes an exclusive lock on `<home>/dispatch.lock`; a second
dispatcher exits 3. It applies pending migrations at start, runs retention once an hour and writes a heartbeat that
`status` and `/healthz` report.

### hookspool run

The intake and the dispatcher in one process, for development and tiny hosts. Production runs the two commands as
separate services so that either can be restarted without the other.

## Looking

### hookspool status

Prints one JSON document: whether the spool is writable, the dispatcher heartbeat, batch counts by state, the oldest
pending age, each destination's breaker state and each source's last event age. It is the same document `/healthz`
serves. `degraded` lists soft problems in words ("dispatcher has not run", dead batches, an open breaker, a silent
source).

### hookspool list

Batches, newest first, one line each, without payloads.

| option | meaning |
|---|---|
| `--state STATE` | one of `open`, `sealed`, `leased`, `retry`, `delivered`, `dead`, `dropped` |
| `--source SOURCE` | only this source |
| `--destination DESTINATION` | only this destination; `quarantine` lists quarantined requests |
| `--limit N` | how many rows (default 50) |

Exit 1 when nothing matches.

### hookspool show

One batch: its state, window, counts, timestamps and last error.

| option | meaning |
|---|---|
| `batch` | the batch id (positional) |
| `--events` | list the batch's events: id, type, size, dedup id, and whether it is excluded |
| `--payload` | include the payloads. They may hold personal data or secrets; a warning is printed to a terminal |
| `--attempts` | include the delivery attempts: outcome, class, status, duration, error |

### hookspool metrics

Prints the same text that `GET /metrics` serves (Prometheus format), without needing the intake to be up.

### hookspool export

Writes batches with their events and attempts as JSON lines to standard output: one batch per line. Each event carries
its payload and, when stored, its raw body (base64), so treat the output as sensitive. It is a forensic and portable copy,
not a restore format. See [OPERATIONS.md](OPERATIONS.md) for backups.

| option | meaning |
|---|---|
| `--state STATE` | only batches in this state (same values as `list`) |
| `--since T` | only batches whose first event arrived at or after `T` (unix seconds) |

## Repairing

### hookspool replay

Sends a dead batch back to `retry`, due now, with its attempt count reset. Its content stays exactly as it was frozen.
A batch replayed this way is delivered ahead of newer batches of its key and so can arrive out of order; the command says so.

On a quarantine batch (a request that a plugin could not classify) `replay` re-runs classification through the plugin as
it is now: each stored request becomes a normal event, and the quarantine batch is marked delivered with the result
`reclassified`. If the plugin still fails, nothing changes.

| option | meaning |
|---|---|
| `batches` | batch ids (positional, any number) |
| `--dead` | every dead batch that matches the filters below (quarantine batches are not included) |
| `--destination D` | with `--dead`: only this destination |
| `--source S` | with `--dead`: only this source |
| `--since T` | with `--dead`: only batches whose first event is at or after `T` (unix seconds) |

### hookspool retry

Takes a batch that is waiting out a backoff (`retry`) and makes it due now. The same command is how you skip the wait
after you fix the thing that was failing.

| option | meaning |
|---|---|
| `batch` | the batch id (positional) |

### hookspool split

Cuts a `sealed`, `retry` or `dead` batch into smaller sealed batches of the same key. The original becomes `dropped`
with the result `split into ...`. Use it when a destination refuses a batch as too large. Give exactly one option.

| option | meaning |
|---|---|
| `batch` | the batch id (positional) |
| `--max-events N` | at most N events per new batch |
| `--max-bytes B` | at most B bytes of payload per new batch |
| `--at EVENT_ID` | two batches: events before `EVENT_ID`, and `EVENT_ID` onward |

### hookspool exclude

Marks events so they are omitted from the batch's document. The event stays stored and listed by
`show --events`; the batch's counts and content hash are recomputed. Use it to remove the one payload that a
destination chokes on. Works on `open`, `sealed`, `retry` and `dead` batches.

| option | meaning |
|---|---|
| `batch` | the batch id (positional) |
| `event_ids` | one or more event ids (positional) |
| `--yes` | required; confirms that you mean it |

### hookspool drop

Moves batches to `dropped`, the operator's discard. Nothing in hookspool ever does this on its own. Dropped batches are
pruned after `retention.dropped_days`.

| option | meaning |
|---|---|
| `batches` | batch ids (positional, any number) |
| `--yes` | required |
| `--reason TEXT` | recorded with the batch |

### hookspool pause

Stops delivery to a destination. Events keep arriving, coalescing and sealing; they wait in the spool. Pausing is
per destination, never per route or source.

| option | meaning |
|---|---|
| `destination` | for example `hermes:github-ci` (positional) |
| `--reason TEXT` | shown in `status` |

### hookspool resume

Lifts a pause (and clears a circuit breaker that was open), so the destination delivers again.

| option | meaning |
|---|---|
| `destination` | the destination name (positional) |

### hookspool issues

Failures are grouped into issues, one per destination and cause. `issues` lists them; the other actions change one.

| option | meaning |
|---|---|
| `action` | `list` (default), `ack`, `resolve` or `ignore` (positional) |
| `id` | the issue id, for `ack`, `resolve` and `ignore` (positional) |
| `--state STATE` | for `list`: only `open`, `acked`, `resolved` or `ignored` issues |

`ack` says you have seen it. `resolve` closes it (it reopens and alerts again if the cause recurs). `ignore` is
permanent silence for that cause: it never alerts or reopens, so use it sparingly.

### hookspool enqueue

Puts one event in the spool by hand, with the same validation as the intake. It is how a maintenance sweep reports
through hookspool and how you test a route without a sender.

| option | meaning |
|---|---|
| `--source S` | required: the source name |
| `--key K` | required: the coalesce key |
| `--destination D` | where to deliver. Default: the `--route`'s destination, else the source's `action` |
| `--route R` | a configured route name |
| `--event-type T` | an event type label |
| `--dedup-id ID` | a dedup id, so a repeat inside the source's window is reported as a duplicate |
| `--file F` | JSON payload file. Default: standard input |

## Checking and maintaining

### hookspool check

Validates the config: unknown keys, bad values, the byte limits that must nest, secret references (readable, and
mode), plugins (imported and bound), destinations, and the Hermes subscriptions file. It also runs SQLite's quick check
on an existing spool. Prints `ok` and exits 0, or prints the errors and exits 2. The service units run it before
starting.

### hookspool reload

Runs `check` and, if it passes, touches the config file so that the running intake and dispatcher both notice and reload
together. An invalid file keeps the previous config in the running services.

### hookspool migrate

Applies pending schema migrations. It takes the dispatcher's lock, so it refuses (exit 3) while a dispatcher runs.

| option | meaning |
|---|---|
| `--dry-run` | print the current schema version and the pending migrations, change nothing |

### hookspool prune

Runs retention now instead of waiting for the hourly pass. It prints how many delivered batches, dropped batches,
events, attempts, dedup rows and issues it deleted. Dead batches are never pruned.

| option | meaning |
|---|---|
| `--vacuum` | also run `VACUUM` to return space to the filesystem. Needs free space of about the size of the spool file |

### hookspool bobbin

Bobbins (see `docs/ARCHITECTURE.md` section 14.3). The subcommand is one of `list`, `show`, `expand`, `new`
and `check`:

| form | does |
|---|---|
| `bobbin list` | the bobbins found (built in, then each `[spool] bobbin_path` directory): name, version, where it comes from, summary, and the `[bobbins.<instance>]` tables that use it |
| `bobbin show <bobbin>` | one bobbin: its secrets, settings (type, required, default), outputs, span, whether the sender retries, the reverse-proxy note |
| `bobbin expand [<instance>]` | the ordinary tables one instance (or every instance) expands to, as TOML. Secret references stay references and `HOOKSPOOL_SECRET_REFS` is shown by secret name only: no secret value is ever read or printed. Exit 2 with every problem when the bobbins do not expand |
| `bobbin new <name>` | a new bobbin directory: a copy of the `webhook` bobbin with its settings frozen into a fixed `[source] template` (`--set` chooses them), an accepted and a rejected capture signed for the scheme, README, SETUP.md and AGENTS.md. It passes `bobbin check` once its TODO placeholders are filled in |
| `bobbin check <dir>` | run the conformance kit on one bobbin directory and print its scorecard: each check, pass / fail / skip, and every problem as `file:line` with how to fix it. Exit 0 when every required check passes, 1 when one fails, 2 when `<dir>` is not a bobbin. It reads no config and no real secret. CI runs this same command on every changed bobbin ([BOBBIN_REVIEW.md](BOBBIN_REVIEW.md)) |

`name` is the bobbin for `show`, the instance for `expand`, the new bobbin's name for `new` and the bobbin
directory for `check`.

| option | meaning |
|---|---|
| `--dir D` | `new`: the parent directory (default: the current directory) |
| `--set NAME=VALUE` | `new`: freeze a webhook setting (`scheme`, `header`, `prefix`, `key`, `dedup`, ...) into the template; the value is TOML (`--set 'key=["id"]'`) or a bare string, an empty value removes the key; repeatable. A value the template refuses stops `new` with the template's message |
| `--code` | `new`: a `source.py` instead of the template |
| `--processor` | `new`: include a `processor.py` and a `[processor]` table |
| `--report text\|md\|json` | `check`: the scorecard as aligned text (default), GitHub Markdown, or JSON (schema `hookspool.bobbin-check/1`) |
| `--strict` | `check`: advisory checks (`scope`) fail the run too |
| `--base REF` | `check`: the git ref the change is compared with; the advisory `scope` check lists files changed outside the bobbin |

### hookspool source

Source tools.

| option | meaning |
|---|---|
| `test` | the only subcommand (positional) |
| `name` | the source name, as in `[sources.<name>]` (positional) |
| `--request FILE` | required: a `.http` capture (format in [ADDING_A_SOURCE.md](ADDING_A_SOURCE.md)) |
| `--now T` | unix seconds to verify at. Default: the capture's `# now:` line, else the current time |

`source test` replays a captured request through the source's verify, handshake, ignore, key, dedup and attrs stages
offline. It never writes to the spool. It prints a JSON report whose `result` is `queued`, `ignored`, `challenge` or
`unverified` (a `queued` report names the `route` and `destination` the event would take, and in `tees` each tee route
that would get a copy), and exits 0 for the first three, 1 for `unverified`, 2 when the capture or the plugin cannot be loaded.
