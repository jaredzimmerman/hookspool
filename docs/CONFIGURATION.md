# Configuration reference

hookspool reads one TOML file, `config.toml`, from the state directory (or wherever `--config` / `HOOKSPOOL_CONFIG`
points). Everything has a default; an absent file means "no sources". `hookspool check` is the judge of a file: unknown
keys are errors there (and warnings at run time, so a typo cannot stop a running service). The service units run
`hookspool check` before they start, so a file it rejects stops a start but not a running service.

The intake and the dispatcher reload the file when its modification time changes. A file that fails validation keeps the
previous configuration active and opens a `config:file` issue. `hookspool reload` validates the file and touches it so
both processes reload together.

This page is checked against the code's key tables by `tests/test_docs.py`: a key that exists in the code and is
missing here fails the suite. A complete working file is [`deploy/config.example.toml`](../deploy/config.example.toml).

Paths follow one rule: command-line option, then environment variable, then this file, then the default. Secrets are
never written in this file; they are references (see [Secrets](#secrets)).

## `[spool]`

| key | default | meaning |
|---|---|---|
| `home` | `/var/lib/hookspool` | state directory: `config.toml`, `spool.db` (and its `-wal`, `-shm`), `dispatch.lock`. `HOOKSPOOL_HOME` and `--home` override it |
| `db` | `spool.db` | spool file name, relative to `home` (`HOOKSPOOL_DB` overrides) |
| `store_raw` | `true` | keep each request's original body, so a quarantined request can be reclassified and a capture re-verified. A source can override it. Turn it off for sources whose bodies you must not keep |

## `[intake]`

| key | default | meaning |
|---|---|---|
| `listen` | `127.0.0.1:8650` | address the intake binds. Keep it on loopback; the reverse proxy is its only client |
| `max_body_bytes` | `1048576` | request bodies above this get 413. A source's own cap is the smaller of this and the `max_bytes` of the routes it feeds, unless the source sets `max_body_bytes` |
| `busy_timeout_s` | `10` | how long a request waits for the database lock before the intake answers 503 |
| `threads` | `16` | request threads |

## `[dispatch]`

| key | default | meaning |
|---|---|---|
| `parallel` | `4` | batches delivered at the same time, across all destinations |
| `poll_s` | `1.0` | how often the dispatcher looks for work. Coalescing windows overshoot by up to one poll |
| `heartbeat_s` | `10` | how often the dispatcher records that it is alive (read by `status` and `/healthz`) |
| `lease_grace_s` | `60` | slack added to a destination's timeout when a batch is leased. A worker gives up 5 seconds before its lease ends, so `check` requires at least 5 |
| `max_attempts` | `64` | counted failures (the destination ran and failed on this batch) before a batch is declared dead. Transport failures are never counted |
| `backoff_base_s` | `15` | first retry delay; it doubles with each failure, with up to 25% jitter on top (the cap is 5 minutes before jitter) |
| `backoff_cap_s` | `300` | the longest retry delay. Long outages are the breaker's job, not the backoff's |
| `breaker` | table | the per-destination circuit breaker, below |

### `[dispatch.breaker]`

| key | default | meaning |
|---|---|---|
| `trip_after` | `3` | consecutive transport failures on a destination before its breaker opens |
| `cooldown_initial_s` | `30` | wait before the first probe of an open breaker |
| `cooldown_max_s` | `600` | the longest wait between probes; the cooldown doubles up to this |

An open breaker pauses delivery to the destination and keeps spooling. It never drops anything. One batch at a time is
sent as a probe; when a probe succeeds the breaker closes and the backlog drains.

## `[coalesce]`

Defaults for the four window fields. A route, a source or a span preset overrides them one field at a time.

| key | default | meaning |
|---|---|---|
| `quiet_s` | `0` | release a batch once no event has joined it for this many seconds |
| `max_s` | `0` | release a batch this many seconds after its first event, however busy it still is. `0` for both `quiet_s` and `max_s` means immediate |
| `max_events` | `500` | seal a batch at this many events, inside the enqueue transaction |
| `max_bytes` | `786432` | seal a batch at this many bytes of payload. `check` requires it to be at most 0.75 of the destination's `max_body_bytes` |

## `[limits]`

| key | default | meaning |
|---|---|---|
| `min_free_bytes` | `536870912` | below this much free disk the intake answers 503 with `Retry-After: 60` instead of accepting what it could not keep |

## `[retention]`

| key | default | meaning |
|---|---|---|
| `delivered_days` | `7` | delivered batches, their events and raw bodies, kept this long after delivery |
| `attempts_days` | `30` | delivery attempt records |
| `dropped_days` | `7` | batches an operator dropped |
| `issues_days` | `90` | resolved and ignored issues |

Dead batches are never pruned. Dedup rows are kept for their source's `dedup_window_s`. Retention runs hourly in small
transactions and on demand with `hookspool prune`.

## `[alerts]`

When an issue opens, hookspool runs `argv` with the alert text appended as the last argument. It is marked sent only when
the command exits 0, so a chat sender that was down is retried.

| key | default | meaning |
|---|---|---|
| `argv` | `["logger", "-t", "hookspool"]` | the command to run. Replace it with whatever reaches you |
| `dry` | `false` | record alerts instead of running the command. `HOOKSPOOL_ALERT_DRY=1` does the same |
| `rate_limit_s` | `1800` | at most one alert per kind in this many seconds |
| `timeout_s` | `60` | kill the command after this long |

## `[hermes]`

Settings for `hermes:<route>` destinations (the webhook listener of a Hermes Agent gateway). Route names, secrets and profiles come from the subscriptions file, read fresh on every attempt.

| key | default | meaning |
|---|---|---|
| `base_url` | `http://127.0.0.1:8644` | where the gateway's webhook listener is |
| `subscriptions_path` | `~/.hermes/webhook_subscriptions.json` | the gateway's routes and their secrets. `check` fails if a route hookspool feeds sets `events` or `coalesce` there |
| `timeout_s` | `30` | per-attempt timeout |
| `max_body_bytes` | `1048576` | the largest body the gateway accepts; keep it equal to the gateway's own limit |
| `legacy_signature` | `true` | also send `X-Hub-Signature-256`, for receivers that only know the GitHub scheme |

## `[spans]`

Named coalescing presets. A span sets `quiet_s` and `max_s`, and a source picks one with `span = "..."`. Add your own.

| preset | `quiet_s` | `max_s` | meant for |
|---|---|---|---|
| `burst` | 120 | 600 | code and CI, where one push triggers many checks |
| `mail` | 30 | 120 | mail, boards and tickets |
| `calendar` | 45 | 180 | calendar edits |
| `sync` | 60 | 300 | bank and data syncs that send several notices per backfill |
| `immediate` | 0 | 0 | meetings, security, anything where latency matters more than batching |

```toml
[spans]
nightly = { quiet_s = 300, max_s = 3600 }
```

Both fields are required in a span.

## `[sources.<name>]`

One table per source. `<name>` is also the URL path: the intake serves `/hooks/<name>`.

| key | default | meaning |
|---|---|---|
| `module` | `hookspool.sources.<name>` | the Python module that implements the source. Use `hookspool.sources.template` for a source defined in config alone |
| `span` | the plugin's own, if any | a preset from `[spans]` |
| `secrets` | none | a table of secret references, one per secret the plugin names (see [Secrets](#secrets)) |
| `action` | the plugin's own | the default destination: `hermes:<route>`, `script:<name>` or an `http` destination's name |
| `dedup_window_s` | `86400` | how long an event id counts as a duplicate. Raising it past `retention.delivered_days` earns a warning |
| `expect_every_s` | none | open a `source_silent` issue after this many seconds without an event; `0` means no expectation |
| `max_body_bytes` | derived | the source's request body cap. `check` requires it to be at most each feeding route's `max_bytes` |
| `keep_headers` | the plugin's own | request headers to store with each event. Authentication headers are refused |
| `store_raw` | `[spool] store_raw` | keep original request bodies for this source |
| `event_header` | none | for a `hermes` destination: the header that carries the event type (the GitHub example sets `X-GitHub-Event`; a header hookspool sets itself is refused) |
| `quiet_s`, `max_s`, `max_events`, `max_bytes` | inherited | window fields for this source (see [Windows](#windows)) |

Further keys depend on the plugin: the Plaid source takes `env`, and the template source takes the keys listed in
[ADDING_A_SOURCE.md](ADDING_A_SOURCE.md). `check` accepts exactly the keys the plugin declares.

## `[[routes]]`

A route sends some of a source's events to a different destination or window. Routes are checked in file order; the first
whose `source` matches and whose `match` is a subset of the event's attributes wins. Events no route claims use the
source's own `action`.

| key | default | meaning |
|---|---|---|
| `name` | required | the route's name, as it appears in logs, `list` and `status`. It must be unique |
| `source` | required | the source this route applies to |
| `match` | `{}` | attribute values that must all be equal (as strings) to the event's attributes. A source's `attrs` supply them; `route_hint` (the proxy's `X-Hookspool-Route` header) is always available |
| `destination` | the source's `action` | where matching events go |
| `quiet_s`, `max_s`, `max_events`, `max_bytes` | inherited | window fields for this route |

```toml
[[routes]]
name = "github-ci"
source = "github"
match = { route_hint = "ci" }
destination = "hermes:github-ci"
quiet_s = 60
```

## Windows

The four window fields are inherited one field at a time, nearest first: route, then source, then the source's `span`
preset (which sets `quiet_s` and `max_s` only), then `[coalesce]`. Setting `quiet_s` on a route therefore leaves the
route's `max_s` coming from the span.

`check` warns when `quiet_s` is greater than `max_s` (the quiet window can never fire) and fails when the byte limits do
not nest: a route's `max_bytes` must fit inside three quarters of its destination's `max_body_bytes`, and a source's
`max_body_bytes` must fit inside each route's `max_bytes`, so that one event can never overflow the document.

## `[destinations.<name>]`

`hermes:<route>` and `script:<name>` exist without a table; a table overrides their defaults. Any other name needs a
table with a `type`.

| key | applies to | default | meaning |
|---|---|---|---|
| `type` | all | from the name | `hermes`, `script` or `http` (not needed for `hermes:` and `script:` names) |
| `module` | all | none | `pkg.mod:ClassName` of a third-party destination (see [ADDING_A_DESTINATION.md](ADDING_A_DESTINATION.md)) |
| `timeout_s` | all | 30 | per-attempt timeout, kill deadline for a script |
| `max_body_bytes` | all | 1 MiB for `hermes` and `http`, unbounded for `script` | the largest document the receiver accepts. `max_bytes` of the routes feeding it must fit in 0.75 of it |
| `max_inflight` | all | 2 for `hermes`, 1 for `script`, 4 for `http` | batches in flight to this destination at once |
| `argv` | `script` | required | the command and its arguments, no shell. The batch document arrives on standard input |
| `cwd` | `script` | none | working directory (absolute path) |
| `env_file` | `script` | none | `KEY=value` file, re-read on every attempt, whose values become the script's environment (absolute path) |
| `state_dir` | `script` | none | a directory the script may keep state in (absolute path). Created if missing; passed as `HOOKSPOOL_STATE_DIR`. List it in the service unit's `ReadWritePaths` |
| `url` | `http` | required | where to POST. Must be `https://` unless `allow_insecure` |
| `secret` | `http` | required | secret reference used to sign each delivery |
| `secret_previous` | `http` | none | the old secret during a rotation; deliveries then carry both signatures |
| `headers` | `http` | none | extra static headers, a table of strings. Headers hookspool sets itself are refused |
| `allow_insecure` | `http` | `false` | permit a plain `http://` URL |
| `unwrap` | `hermes`, `http` | `false` | deliver each event on its own, as the sender sent it, instead of the batch document (see [Unwrap](#unwrap-one-request-per-event)) |
| `event_header` | `hermes`, `http` | none | with `unwrap`: the header that carries each event's type (for example `X-GitHub-Event`). A `hermes` destination without it falls back to the source's `event_header` |
| `dedup_header` | `hermes`, `http` | none | with `unwrap`: the header that carries each event's sender-side dedup id when it has one (for example `X-GitHub-Delivery`) |

`quarantine` is a reserved name that never delivers.

### Unwrap: one request per event

Some receivers expect the sender's original payload, one request per event, not hookspool's batch document: a Hermes
route whose prompt template reads `{pull_request.title}`, or an endpoint written for the sender itself. `unwrap = true`
keeps everything hookspool adds (commit before the sender's 2xx, retry until accepted, the breaker, issues) and changes
only what goes on the wire:

- each event of a batch is its own POST, in event order; the body is the event's stored payload
- `webhook-id` is `hs_<instance>_<batch>_<event id>`, the same on every attempt, with the Standard Webhooks signature and
  `X-Hub-Signature-256` computed over that body; `event_header` and `dedup_header` add the event's type and dedup id
- the batch is delivered only when every event's request succeeded. The first failure stops the attempt and the whole batch
  is retried (or goes dead) exactly as a document would; events the receiver already accepted are sent again with the same
  id, which a receiver that keeps ids (Hermes does) answers `duplicate`
- for `hermes`, an answer of `ignored` or `coalesced` is the gateway's own filter or coalescing at work and opens no issue,
  and `check` allows the gateway route's `events` filter and `coalesce` block (they see original events now)

Use it with coalescing off for the route (`quiet_s = max_s = 0`, the `immediate` span), so each event is its own batch.
With a window, every event is still delivered in order, but the receiver gets them as a burst; `check` warns.

```toml
[destinations."hermes:github-releases"]
unwrap = true
event_header = "X-GitHub-Event"
dedup_header = "X-GitHub-Delivery"
```

```toml
[destinations."script:digest"]
argv = ["/usr/bin/python3", "/opt/scripts/digest.py", "--batch"]
env_file = "/etc/hookspool/digest.env"
state_dir = "/var/lib/hookspool/scripts/digest"
timeout_s = 300

[destinations.audit]
type = "http"
url = "https://audit.example.com/hooks/in"
secret = { file = "/etc/hookspool/secrets/audit" }
headers = { "X-Env" = "prod" }
```

## Secrets

A secret in this file is always a reference to where the value lives, never the value. A literal string is refused.

| form | reads from | rotates without a restart |
|---|---|---|
| `{ file = "/path" }` | the file's contents, trailing newline removed | yes |
| `{ env_file = "/path", key = "NAME" }` | the `NAME=value` line of a `KEY=value` file | yes |
| `{ env = "NAME" }` | the process environment | no (`check` warns) |

References are resolved when they are used, never cached across requests, never logged and never written to the
database. `check` verifies that each file exists, is non-empty and is not readable by group or others (mode 0600 is the
norm).

## Removed settings

Settings from earlier drafts are refused with a pointed message rather than ignored: `limits.max_pending_batches`,
`limits.max_db_bytes`, `limits.plugin_error_streak`, `retention.archive_dir`, `dispatch.poison_after`,
`dispatch.dead_after_s`, `delay_s`, `rate_per_min`, `idempotency` and `paused`. Rate limiting belongs to the reverse
proxy, and there is no archive: `VACUUM INTO` and `hookspool export` are the backup (see [OPERATIONS.md](OPERATIONS.md)).
