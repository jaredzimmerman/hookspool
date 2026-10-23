# Operating hookspool

This is the day-two guide: how to tell that it is healthy, what each alert means, and what to do about a batch that is
stuck. Installing it is in [`deploy/README.md`](../deploy/README.md), every command is in [CLI.md](CLI.md), and the design
reasons are in [ARCHITECTURE.md](ARCHITECTURE.md).

## The model in one paragraph

An event is committed to the spool before its sender gets a 2xx. Events with the same key join one **batch**, which is
**sealed** when its window closes (quiet period, maximum wait, event count or byte size, whichever comes first) and then
delivered to its destination, oldest first within a key and in parallel across keys. A batch moves through these states:

| state | meaning | what happens next |
|---|---|---|
| `open` | still collecting events | seals when its window closes |
| `sealed` | frozen, waiting to be delivered | the dispatcher leases it |
| `leased` | a worker is delivering it right now | delivered, retried, or dead |
| `retry` | the last attempt failed; waiting for the backoff | tried again, with no limit for outages |
| `delivered` | the destination took it | pruned after `retention.delivered_days` |
| `dead` | will not be retried without a person | waits for you; never pruned |
| `dropped` | an operator discarded it (or `split` replaced it) | pruned after `retention.dropped_days` |

Nothing is ever discarded automatically. A batch is `dead` only if the destination rejected the document itself
(a 400, 413, 415 or 422), if it failed `dispatch.max_attempts` times on its own merits, or if it is a poison batch: one that
failed six times in a row while the destination kept accepting other batches between its attempts. Transport failures (the destination is down) never count against a batch and never kill it.

## Is it healthy?

```
hookspool status
curl -s http://127.0.0.1:8650/healthz
```

They return the same JSON. `/healthz` is HTTP 200 when the spool is writable and the disk has room, and 503 otherwise;
soft problems appear in `degraded` while it stays 200. Do not expose either to the internet; the example nginx config
serves only `/hooks/*`.

| field | what to read in it |
|---|---|
| `ok` | `false` means the spool cannot take events: unwritable, or free disk below `limits.min_free_bytes` |
| `degraded` | plain-language soft problems: the dispatcher has not run, a heartbeat older than 30 s, dead or quarantined batches, an open breaker, a silent source |
| `dispatcher.alive`, `heartbeat_age_s` | whether the dispatcher is running; it writes a heartbeat every 10 s |
| `batches` | counts by state. `dead` and `quarantined` should be zero |
| `oldest_pending_age_s` | how long the oldest undelivered batch has waited. This is the number that says "something is stuck" |
| `destinations.<name>` | the breaker state (`closed`, `half_open`, `open`, `paused`), in-flight count, and for a failing destination the last error |
| `sources.<name>` | seconds since its last event, and `silent` when it has exceeded its `expect_every_s` |
| `issues_open` | open issues (below) |

### Metrics

`GET /metrics` on the intake, and `hookspool metrics` without it. Prometheus text format, nothing persisted.

- Counters, per intake process (they reset on restart, which `rate()` handles): `hookspool_events_received_total`,
  `hookspool_events_duplicate_total`, `hookspool_events_quarantined_total` and `hookspool_requests_rejected_total`
  (labelled by source and reason: `unverified`, `too_large`, `chunked`, `spool_full`, `busy`, `ignored`).
- Gauges computed from the spool at scrape time: `hookspool_batches{state}`, `hookspool_oldest_pending_age_seconds`,
  `hookspool_destination_state{destination}` (0 closed, 1 half open, 2 open, 3 paused),
  `hookspool_destination_oldest_pending_age_seconds`, `hookspool_issues_open`, `hookspool_spool_bytes`,
  `hookspool_disk_free_bytes`, `hookspool_dispatcher_heartbeat_age_seconds` and
  `hookspool_source_last_event_age_seconds{source}`.
- Delivery figures are gauges over the retention window, not counters, because a counter computed from pruned tables would
  not be monotonic: `hookspool_attempts_30d{destination,outcome,klass}`, `hookspool_batches_delivered_7d{destination}`,
  `hookspool_attempt_duration_seconds_sum_30d` and `..._count_30d`.

Alert on `hookspool_oldest_pending_age_seconds`, `hookspool_dispatcher_heartbeat_age_seconds` and
`hookspool_batches{state="dead"}`. They cover most failures.

### Logs

Logs go to standard error (journald captures them). `HOOKSPOOL_LOG=json` writes one JSON object per line with fixed fields:
`ts, level, component, msg, source, route, batch, event, destination, attempt, status, bytes, duration_ms, reason`. Logs hold
ids, names, sizes, states, codes and durations, never request bodies, headers, secrets or query strings, and there is no
debug level that prints them. The test suite plants a marker in every payload and secret and fails if a log line carries it.

## Issues and alerts

Failures are grouped into **issues**, one per cause, so a destination that is down for an hour is one issue and one alert,
not one per batch.

| kind | opened by | closes when |
|---|---|---|
| `delivery` (per destination and cause: unreachable, http 5xx, timeout, script exit, permanent, poison, exhausted, ignored) | a batch going dead, a breaker opening, three counted failures in a row, a Hermes answer of `ignored` or `coalesced` | a delivery to that destination succeeds. For permanent, poison and exhausted: when the dead batches for it are replayed or dropped. For `ignored`: when you resolve it |
| `source_silent` | no event from a source for longer than its `expect_every_s` | the next event arrives |
| `spool` | a backpressure limit engaged (low disk, a write that failed) | the limit clears |
| `config` | a config reload that failed validation; a plugin that raised on a verified request (the request is quarantined); a destination answering 401, 403 or 404 | the condition is fixed, a reload succeeds, or the quarantine batches are reclassified or dropped |

```
hookspool issues                    # list (add --state open to filter)
hookspool issues ack 12             # seen it, working on it
hookspool issues resolve 12         # done (it reopens and alerts again if it recurs)
hookspool issues ignore 12          # silence this cause for good; use sparingly
```

Each open issue alerts once, by running `alerts.argv` with the text as its last argument, and again only if it reopens.
It counts as sent only when the command exits 0, so a chat sender that was down is retried. At most one alert per kind goes
out every `alerts.rate_limit_s` (30 minutes). A dead batch's issue names the command to replay it.

## When a batch is stuck

Start from the symptom.

| you see | likely cause | do |
|---|---|---|
| `oldest_pending_age_s` growing, a destination `open` | the destination is down or refusing connections; the breaker stopped hammering it | fix the destination. A probe goes out every 30 s to 10 min and the breaker closes by itself. Nothing is lost while it is open. Once it is back, `hookspool resume <destination>` closes the breaker at once instead of waiting for the next probe |
| a destination `paused` | an operator paused it (`hookspool pause`) | `hookspool resume <destination>` |
| batches in `retry` with `last_class` config | a 401, 403 or 404 from the destination, an unknown route, an unreadable secret or env file | read the issue and `hookspool show <id> --attempts`; fix the secret or route; `hookspool reload` makes them due at once |
| batches in `retry`, a script destination | the script exited non-zero on this batch (counted), or timed out (not counted) | `hookspool show <id> --attempts` for the exit status; fix the script. It retries on its own; `hookspool retry <id>` skips the wait |
| a `dead` batch, `permanent` | the receiver rejected the document: too large (413) or malformed | `split` or `exclude` (below), then `replay` |
| a `dead` batch, `poison` or `exhausted` | one batch keeps failing a destination that is otherwise fine | look at its events with `show --events`, `exclude` the bad one, `replay` |
| `quarantined` batches | a plugin raised on a verified request, so it was kept instead of failing the sender | fix the plugin or the source table, then `replay` the quarantine batches: each stored request is classified again |
| a source `silent` | nothing arrived within `expect_every_s`: the sender stopped, the URL changed, or a watch lapsed | check the sender's side; run the source's sweep (see `deploy/README.md`) |
| `ok: false` | the spool cannot take writes: disk below `limits.min_free_bytes`, or the database is locked or unwritable | free disk space on the volume (retention alone does not shrink the file; see Retention), check permissions. The intake answers 503 meanwhile, and senders that retry will |
| the dispatcher `alive: false` | not running, or wedged | `systemctl status hookspool-dispatch`. A second dispatcher started by hand exits with 3 because the first holds the lock |

### Looking at a batch

```
hookspool list --state dead
hookspool show 41                    # state, window, counts, last error
hookspool show 41 --events           # each event: id, type, size, dedup id, excluded or not
hookspool show 41 --attempts         # each delivery attempt: outcome, class, status, duration
hookspool show 41 --payload          # the payloads themselves (may hold personal data)
```

### Replay

```
hookspool replay 41                                   # one dead batch
hookspool replay --dead                               # every dead batch
hookspool replay --dead --destination hermes:github-ci --since 1790000000
```

A replayed batch goes back to `retry`, due now, with its attempt count reset and its content unchanged. It is delivered
ahead of newer batches of its key, which means it can arrive out of order: events of the same key that were delivered
while it was dead will already have been seen. Receivers must tolerate that, and the command says so.

### Split and exclude

Use these when the receiver cannot take a batch whole.

```
hookspool split 41 --max-events 100        # or --max-bytes 200000, or --at <event id>
hookspool exclude 41 9912 --yes            # leave one event out of the document
```

`split` cuts a `sealed`, `retry` or `dead` batch into smaller sealed batches of the same key and marks the original
`dropped` with the result `split into ...`. `exclude` omits events from the batch's document and recomputes its counts and
content hash; the event itself stays stored and shows in `show --events` as excluded. After either, `replay` a dead batch
or let a waiting one deliver.

### Drop

```
hookspool drop 41 --yes --reason "duplicate of a hand-sent fix"
```

`drop` is the operator's discard, the only way a batch is ever thrown away. It is recorded with the reason, kept for
`retention.dropped_days`, and then pruned. A redelivery by the sender of an event whose batch you dropped is accepted as a
new event; one whose batch is dead is treated as a request to retry it.

### Pause and resume

```
hookspool pause hermes:github-ci --reason "gateway upgrade"
hookspool resume hermes:github-ci
```

A paused destination stops being delivered to. Events keep arriving, coalescing and sealing; sealing is a property of the
batch, not the destination, so a long pause produces a queue of bounded, sealed batches and not one growing document.
Pause is per destination only. A route or source has no runtime switch, because pausing its destination does the same.

## Sending an event by hand

```
echo '{"note": "hello"}' | hookspool enqueue --source manual --key ops --destination hermes:ops --dedup-id once-2026-10-23
```

It is validated exactly like intake traffic, coalesces with everything else on the same key, and shows up in `list`.
Maintenance sweeps use it to report what the push path missed.

## Retention

Retention runs in the dispatcher once an hour, in transactions of at most 1,000 rows so the intake never waits long on it.

| data | kept | setting |
|---|---|---|
| delivered batches, their events and raw bodies | 7 days after delivery | `retention.delivered_days` |
| delivery attempts | 30 days | `retention.attempts_days` |
| dropped batches | 7 days | `retention.dropped_days` |
| resolved and ignored issues | 90 days | `retention.issues_days` |
| dedup records | the source's `dedup_window_s` (24 hours by default) | per source |
| **dead batches** | **never** | you replay or drop them |

After a pass hookspool checkpoints the write-ahead log. It does not run `VACUUM`: that needs free space equal to the
database and blocks writers, so it is manual.

```
hookspool prune               # run retention now; prints what it deleted
hookspool prune --vacuum      # also return freed space to the filesystem (stop the dispatcher first on a small disk)
```

The database does not shrink by itself after a large prune. That is SQLite reusing its own free pages, not a leak.

## Backup and restore

The spool is one SQLite file in WAL mode. **Do not copy `spool.db` while the services run**: the recent writes live in the
`-wal` file beside it, and a copy of the main file alone is not a consistent database. Use SQLite's own copy, which is
consistent and online:

```
umask 077
sqlite3 /var/lib/hookspool/spool.db "VACUUM INTO '/backups/hookspool-$(date +%F).db'"
```

`VACUUM INTO` writes a fresh, compact, valid database. Make sure the target directory is private: the backup holds your
payloads and raw bodies. (`sqlite3` is the standard command-line shell; from Python,
`sqlite3.connect(path).execute("VACUUM INTO ?", (target,))` does the same.)

For a readable, portable copy that you can inspect or replay into another system, export JSON lines:

```
hookspool export --state delivered --since 1790000000 > delivered.jsonl
hookspool export --state dead > dead.jsonl
```

Each line is a batch with its events (payload and, if stored, the raw body as base64) and its attempts. `export` is for
forensics and migration, not a restore format; the backup file is the restore format. Treat both as sensitive.

There is no built-in archive, deliberately: a copy of the data lives wherever your backup policy puts it.

### Restoring

Stop the services, then set the damaged database aside **together with its `-wal` and `-shm` files**. After a crash the
`-wal` file holds committed events that were never checkpointed into the main file; deleting it throws them away.
SQLite pairs a database with its sidecars by name, so rename all three to match:

```
systemctl stop hookspool-intake hookspool-dispatch
cd /var/lib/hookspool
mv spool.db spool.db.broken
mv spool.db-wal spool.db.broken-wal ; mv spool.db-shm spool.db.broken-shm   # each may not exist; that is fine
```

Recover what you can **before** restoring, while the set-aside copy is still the freshest record of what was accepted.
Point `export` at it with `HOOKSPOOL_DB`; opening it replays the `-wal` file, so the uncheckpointed events come out too:

```
HOOKSPOOL_DB=/var/lib/hookspool/spool.db.broken hookspool export > recovered.jsonl
```

(Or take `sqlite3 spool.db.broken "VACUUM INTO '/backups/recovered.db'"`. Work on a copy of the three files if you want
the originals untouched.) If the file is too damaged to open, the export fails and you still hold the set-aside files.
Then restore:

```
cp /backups/hookspool-2026-10-22.db spool.db
chown hookspool: spool.db ; chmod 600 spool.db
hookspool check
systemctl start hookspool-dispatch hookspool-intake
```

Restoring returns the spool to the moment of the backup, so two things follow. Batches delivered since then are delivered
again (delivery is at-least-once; HTTP and script receivers can drop a repeat by its `webhook-id`, which is stable per
batch, while a Hermes route may wake twice). And events accepted since the backup are gone from the live spool (the ones you exported above are in `recovered.jsonl`); a sender that
retries will redeliver the ones that failed, and a sweep reconciles the rest.

If the database fails SQLite's integrity check, `hookspool check` fails and the dispatcher refuses to start rather than
carry on, and nothing will "move aside and start fresh" on its own (the intake's unit runs `check` first, so it stops too). Recover what you can with `hookspool export`
from the readable parts (as above), then restore from backup.

## Upgrades

Install the new code, stop the dispatcher, run `hookspool migrate` (add `--dry-run` first to see what is pending), start
the dispatcher, restart the intake. The sequence, and what to do about the senders during it, is in section 5 of
[`deploy/README.md`](../deploy/README.md). Take a backup first when a release carries a migration. A schema newer than the
code (a downgrade) is refused with exit 2.

## Rehearsing a failure

Before relying on any of this, run the gateway-down drill in [`deploy/README.md`](../deploy/README.md) (section 3): it
makes a destination unreachable, feeds events, confirms the breaker opens, nothing grows past its bounds, nothing is
lost, and everything is delivered in order once the destination returns.

## What you should not do

- Do not edit the database with `sqlite3`. Every state change has a command, and the commands keep the invariants
  (counts, hashes, issue links) that a hand edit would break.
- Do not start a second dispatcher against the same spool. It will exit; do not work around the lock.
- Do not delete `spool.db-wal` while a service runs.
- Do not put `/healthz` or `/metrics` on the public internet, or bind the intake to a public address.
- Do not set `alerts.argv` to a command that blocks for long; it runs on a worker thread and is killed after `alerts.timeout_s`.
