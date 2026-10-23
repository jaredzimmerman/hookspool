# hookspool architecture (v2, revised after review)

hookspool is a small, self-hosted, durable, coalescing intake for **inbound** webhooks. It sits between a reverse proxy and whatever consumes the events (an AI agent gateway such as Hermes Agent, a script, or any HTTP endpoint). It is built for one modest host: Python 3.11+, the standard library plus `cryptography` and `PyJWT`, one SQLite file, no LLM, low CPU.

Three promises:

1. **Nothing is lost.** An event is committed to disk before its sender gets a 2xx, delivered at least once, and can be replayed. A busy or dead destination delays work; it never discards it. Our own bugs never turn a verified request into a 5xx: they quarantine it.
2. **One behaviour for every source.** A source plugin supplies only its verifier, its coalesce key, its dedup id, its destination, an optional handshake and a coalesce span. Spooling, dedup, coalescing, leases, retries, breakers, issues, alerts, metrics and the CLI are shared and have no per-source variants.
3. **Bursts become one wake-up.** Events are coalesced per `(route, key)` with a quiet window and a maximum wait that follow the service type, so a destination that costs money or CPU per call (an agent run) is called once per burst, with every event in order. A batch is bounded in events and bytes whatever the destination is doing.

This document is the contract for the v2 build. Where it conflicts with code, the code is wrong. Section 20 records how the architecture review (`.ladder/ARCH_REVIEW.md`) was addressed.

---

## 1. Prior art: what was taken, changed or rejected

| Concept | Source | Decision | Why |
|---|---|---|---|
| Events vs deliveries as separate records | Convoy | **Adopted** as `events` / `batches` / `attempts` | An event is an immutable fact; a batch is one unit of delivery; an attempt is one try. Keeping them apart makes replay, audit and metrics trivial. |
| Idempotent ingestion by key | Convoy | **Adopted** | `dedup_id` per source inside a window; duplicates are counted, not stored. |
| Endpoint circuit breaking | Convoy | **Adapted** | Convoy trips on a failure *rate* over a window and *discards* new events while an endpoint is inactive. hookspool trips on a short run of consecutive transport failures (volumes here are tens of events a day, so rates are meaningless) and **never discards**: open breaker means "stop hammering, keep spooling, probe with a rotating batch". |
| Retention policies | Convoy | **Adapted** | Delivered batches and attempts are pruned on a schedule. Dead batches are never auto-pruned. There is no archive option: `hookspool export` and `VACUUM INTO` are the backup. |
| `webhook-id` / `webhook-timestamp` / `webhook-signature` on outbound deliveries | Standard Webhooks, Svix | **Adopted** | Every delivery hookspool makes carries these headers, signed `v1,<base64 HMAC-SHA256>` over `id.timestamp.body`. `X-Hub-Signature-256` is sent too for receivers that only know the GitHub scheme. |
| Message attempts log | Svix | **Adopted** | `attempts` table, visible in `hookspool show`. |
| FIFO endpoints | Svix | **Adapted** | Strict FIFO per `(route, key)`, not per endpoint; keys deliver in parallel. A dead batch leaves the queue so one poison payload cannot block its key forever. |
| Throttling, `Retry-After` | Svix, Standard Webhooks | **Adapted** | Per-destination `max_inflight`; `Retry-After` is honoured; 429 is a throttle signal, not a breaker failure. There is no hookspool-side rate limit: inbound rate limiting belongs to the reverse proxy, outbound to the receiver's `Retry-After`. |
| Multi-day retry schedule (up to ~75 h) | Standard Webhooks, Svix | **Rejected** | Long outages are the breaker's job, not backoff's. Backoff is capped at 5 min so recovery is fast; the breaker stops the hammering. Batches never die from the passage of time, and transport failures never count against a batch. |
| Disable an endpoint after N days of failure | Svix | **Rejected** | hookspool pauses (operator or breaker) and keeps spooling; nothing is ever turned off automatically for good. |
| `webhook-delivery: abort-message` | Svix | **Rejected** | Receivers are ours; a 4xx says the same thing. |
| Connections = source → destination + rules | Hookdeck | **Adapted** as **routes** | A route is `(source, match)` → `destination` plus a coalesce window. A plugin supplies the default destination; config can override by attributes (for example GitHub repo and class). |
| Filters | Hookdeck | **Adapted** | A plugin's `ignore()` (ping, sync handshakes) and a route's `match` table. No expression language. |
| Transformations | Hookdeck | **Rejected** | Plugins shape the payload in Python; anything heavier is a `script:` destination. |
| Issues that group failures | Hookdeck | **Adopted** | Failures are fingerprinted per `(destination, reason class)`; one issue, one alert, auto-resolve on success. |
| Pause / resume a connection | Hookdeck | **Adopted** for destinations | `hookspool pause <destination>` and `resume`. Paused means spool only. Routes are pure configuration and have no runtime state to pause; pausing the destination covers the real case. |
| Bulk retry | Hookdeck | **Adopted** | `hookspool replay --dead [--destination X] [--since T]`. |
| Delay rule | Hookdeck | **Rejected** for now | No source in the first rollout needs a delivery delay. `not_before` stays in the schema for replay; a `delay_s` route option can be added when a user appears. |
| Field-based dedup windows | Hookdeck | **Adapted** | The plugin computes the id from fields it knows; the window is per source (default 24 h). |
| Debounce vs batching do not compose | Inngest | **Adapted** | Inngest's debounce keeps only the *last* event, batching keeps *all*; the two conflict. hookspool's coalescing is a debounce that keeps every event, so there is nothing to compose: one mechanism, released by quiet window, max wait, max events or max bytes. |
| Batch max size | Inngest | **Adopted** as a hard bound | `max_events` (default 500) and `max_bytes` (default 768 KiB) seal a batch in the enqueue transaction itself, so a batch never exceeds them no matter how long its destination is down. |
| Reactive threshold triggers ("N events within T") | Prefect | **Rejected** | The destination receives `count`; thresholds belong there. |
| Proactive triggers (absence of an event within T) | Prefect | **Adapted** | `expect_every_s` on a source opens an issue when a source goes silent (watch channels expire; subscriptions get dropped). This also formalises the maintenance-sweep convention (section 13). |
| Hermes' own in-memory `coalesce` and `events` filter | Hermes Agent | **Rejected** for routes fed by hookspool | `coalesce` keeps the last event only and lives in memory; `events` filters a whole batch on one event type. `hookspool check` refuses both on routes it feeds; per-type routing belongs in `attrs` + routes. |

---

## 2. Components and process model

```
                 ┌────────────────────────────── host ───────────────────────────────┐
  senders ──TLS──▶ reverse proxy ──▶ hookspool-intake (127.0.0.1:8650)               │
                 │                       verify → classify → enqueue (commit) → 2xx   │
                 │                                  │                                 │
                 │                                  ▼                                 │
                 │                     spool.db (SQLite, WAL, synchronous=FULL)       │
                 │                                  ▲                                 │
                 │                                  │ seal / lease / attempt / mark   │
                 │                       hookspool-dispatch (one process)             │
                 │                       coalesce windows, FIFO per key, breakers,    │
                 │                       retries, issues, alerts, retention           │
                 │                                  │                                 │
                 │            ┌─────────────────────┼─────────────────────┐           │
                 │            ▼                     ▼                     ▼           │
                 │     hermes:<route>          script:<name>         http:<name>      │
                 │     (agent gateway)      (durable emits back)   (generic, signed)  │
                 └────────────────────────────────────────────────────────────────────┘
                   hookspool CLI: status, list, show, replay, split, exclude, pause, issues, ...
```

**Two long-running processes, one database.**

- `hookspool intake`: a `ThreadingHTTPServer` bound to loopback only, behind the reverse proxy. Each request runs verify → classify → one `BEGIN IMMEDIATE` transaction → response. It has no timers and no in-memory state that matters (per-process metric counters only); it can be restarted at any time and the proxy returns 502 to senders. It never applies schema migrations: on a database older than it knows it exits 2 and says to run the dispatcher or `hookspool migrate`.
- `hookspool dispatch`: the only process that delivers and the only long-running process that writes schema. One main loop thread, a small worker pool (`dispatch.parallel`, default 4), a maintenance tick every 60 s. It holds an exclusive `flock` on `<state>/dispatch.lock`; a second instance exits 3. It writes a heartbeat row every `dispatch.heartbeat_s` (10 s) so the intake's `/healthz` can report a dead dispatcher.
- `hookspool <cmd>`: the CLI, short-lived, same database. `hookspool run` runs intake and dispatch in one process (dev, tests, tiny hosts). `hookspool migrate` takes `dispatch.lock` too, so it cannot run beside a live dispatcher.

Why two processes: the intake must keep answering senders while the dispatcher is restarted, upgraded, or wedged in a script; systemd restarts them independently; their failure domains differ (network-facing vs subprocess-running).

**State directory** (`HOOKSPOOL_HOME`, `--home`, or config): `/var/lib/hookspool` by default; on a Hermes host `~/.hermes/state/hookspool` is the conventional choice. It holds `config.toml`, `spool.db` (+ `-wal`, `-shm`) and `dispatch.lock`.

**SQLite settings**: `journal_mode=WAL`, `synchronous=FULL` (commit-before-ack is the whole point), `foreign_keys=ON`, `busy_timeout` 10 s in the intake (then 503) and 30 s in the dispatcher and CLI, one connection per thread, every write in `BEGIN IMMEDIATE`. The dispatcher runs `PRAGMA wal_checkpoint(TRUNCATE)` after each retention pass and `PRAGMA quick_check` at startup.

**Clock**: every row stores wall-clock seconds, and rows are never rewritten to follow the clock. The dispatcher computes `now_eff = max(wall_clock, meta.last_tick_at)` once per tick and uses it for readiness, due times and lease expiry; a backwards NTP step therefore makes time stand still (bounded delay, no writes) until the wall clock catches up. `last_tick_at` is written with the heartbeat (every `heartbeat_s`, 10 s), not on every poll, so after a restart `now_eff` may trail the last real tick by up to one heartbeat; that only shortens the standstill after a backwards step that lands in a restart window, and no row is affected. The intake uses the wall clock as is.

---

## 3. Data model

All timestamps are Unix seconds as REAL. Ids are integers. Sizes are bytes.

### `meta`
`key TEXT PRIMARY KEY, value TEXT`. Keys: `schema_version`, `instance_id` (random, minted once; used in outbound `webhook-id`), `dispatcher_heartbeat_at`, `dispatcher_pid`, `last_tick_at` (clock guard, section 2), `last_enqueue_at`, `source:<name>:last_event_at`.

### `events`
One row per accepted webhook. Immutable after insert, except `excluded_at` (operator only).

| column | notes |
|---|---|
| `id` | PK |
| `source` | plugin name |
| `route` | resolved route name (the source name for the implicit route and for quarantine) |
| `batch_id` | → batches (enforced by `foreign_keys=ON`) |
| `event_type` | plugin-supplied, free text; delivered as `event_type` |
| `dedup_id` | plugin-supplied or NULL |
| `received_at` | |
| `payload` | JSON text, what the destination receives; NULL for a quarantined event (classification failed) |
| `raw` | BLOB, the original body, or NULL when the source sets `store_raw = false`; always kept for a quarantined event |
| `headers` | JSON object of the headers named in the source's `keep_headers` allow-list; for a quarantined event, every request header except the source's `auth_headers` |
| `size` | `len(raw)` or `len(payload)` |
| `excluded_at` | NULL, or when an operator ran `hookspool exclude`; an excluded event stays stored, is omitted from the document and listed by `show --events` |

Indexes: `(batch_id, id)`, `(source, received_at)`.

### `dedup`
`source, dedup_id, event_id, seen_at, dup_count`; `PRIMARY KEY (source, dedup_id)`. No foreign key on `event_id` (the dedup window may outlive the event's retention). Rows older than the source's `dedup_window_s` are pruned, which is how the window works. A duplicate increments `dup_count` and returns the existing `event_id`.

### `batches`
One unit of delivery to one destination.

| column | notes |
|---|---|
| `id` | PK |
| `route`, `source`, `destination` | denormalised for queries; `destination = "quarantine"` for a quarantine batch |
| `key` | coalesce key (`"quarantine"` for a quarantine batch) |
| `state` | `open`, `sealed`, `leased`, `retry`, `delivered`, `dead`, `dropped` |
| `first_at`, `last_at` | first and most recent event; immutable facts |
| `quiet_s`, `max_s`, `max_events`, `max_bytes` | the window this batch was opened with (config changes do not move open batches) |
| `not_before` | from replay; NULL otherwise |
| `count`, `bytes` | denormalised, maintained in the enqueue transaction |
| `sealed_at` | when the batch stopped accepting events (section 5); set in the enqueue transaction (size) or the dispatcher tick (readiness) |
| `content_hash` | SHA-256 over the canonical JSON of `events` + `event_meta` (the frozen part of the document, excluded events omitted); set at seal, recomputed by `split`/`exclude`; identical on every attempt |
| `attempts` | counted attempts (section 6.5) |
| `next_attempt_at` | for `retry` |
| `lease_until`, `lease_owner` | for `leased`; `lease_owner` fences every mark (section 6.2) |
| `last_error` | short, scrubbed; `reason_class` of the last outcome in `last_class` |
| `last_class` | `transport`, `throttle`, `batch`, `config`, `permanent`, `poison`, `quarantine`, NULL |
| `delivered_at`, `result` | |

Indexes: `(route, key, state)` for the open-batch lookup; `(state, next_attempt_at)`; `(destination, state)`; `(state, delivered_at)` for retention.

### `attempts`
One row per try, written *before* the request is made and completed after.

`id, batch_id, n, destination, started_at, finished_at, outcome (ok|retry|dead|discarded), klass, status (HTTP code or exit code), error, duration_ms, probe (0/1), counted (0/1), delivery_id (the webhook-id sent), events_delivered, failed_event_id`. `discarded` means the worker finished after losing its lease and its result was not applied. `events_delivered` and `failed_event_id` (schema 2) are set only for an `unwrap` delivery (section 6.4): how many events this attempt delivered, in order, and the event whose request failed; ids and counts only, never a body.

### `destinations`
Runtime state, one row per destination name (created on first use).

`name, state (closed|open|half_open|paused), consecutive_failures, opened_at, cooldown_s, probe_until, last_probe_batch_id, paused_at, paused_by, last_ok_at, last_error, retry_after_until`. In-flight counts are kept in the dispatcher's memory (it is the only process that delivers).

### `issues`
`id, fingerprint UNIQUE, kind (delivery|source_silent|spool|config), destination, route, state (open|acked|resolved|ignored), first_at, last_at, count, sample_batch_id, last_error, alerted_at`.

### `alerts`
`kind PRIMARY KEY, last_at, suppressed` — the persisted rate limit for outbound alerts.

### `sources`
Not a table. Sources and routes are configuration. Per-source runtime state that must persist (last event time for `expect_every_s`) lives in `meta`.

---

## 4. State machines

### 4.1 Batch

```
                 enqueue (first event for a (route,key) with no open batch)
                              │
                              ▼
                            open ── events join; last_at/count/bytes grow
                              │
          seal: enqueue hits max_events/max_bytes, or the tick finds it ready
                              │
                              ▼
   ┌────────────────────── sealed ── content frozen; content_hash set
   │                          │
   │              gate open: claim (lease_until = now + timeout + grace)
   │                          ▼
   │                        leased
   │                          │
   │   ├── ok ───────────────▶ delivered (emits enqueued in the same txn)
   │   │
   │   ├── retry (transport | throttle | batch | config) ──▶ retry ── due ──▶ leased
   │   │
   │   ├── permanent, poison, max_attempts of counted ──▶ dead ──┐
   │   │                                                          │ replay: dead → retry,
   │   └── lease expired (dispatcher died) ──▶ retry, due now     │ due now, attempts = 0
   │                                                              ▼
   └──── split ──▶ new open batches (operator)                  retry

   open | sealed | retry | dead ── hookspool drop --yes ──▶ dropped   (operator only)
```

Rules:
- An event joins **the** `open` batch for its `(route, key)` (there is at most one, because a batch leaves `open` only by sealing); otherwise it opens one. A batch in any other state never receives events. Once sealed, a batch's content is frozen and `content_hash` is set, so every attempt carries the same events in the same order. Only `split` and `exclude`, run by an operator, change a sealed batch's content, and they recompute the hash.
- `dead` is reached only by a **permanent** outcome, by the poison rule (section 4.2), by exhausting `max_attempts` of **counted** attempts, or by quarantine at intake (section 9). Wall-clock time never kills a batch, and neither does an unreachable destination: transport failures are not counted.
- `sealed`/`retry` → `leased` requires the destination gate to be open (section 6.3). While a destination is `open` or `paused`, sealed batches wait with no attempt rows written, and new events open fresh batches behind them, each bounded by `max_events`/`max_bytes`.
- `replay` moves a `dead` batch to `retry` due now with `attempts = 0`; it does not reopen it (its content stays frozen). Replaying a quarantine batch is different: it re-runs classification (section 9).
- `dropped` exists so an operator can discard a batch deliberately and the audit trail says so. Automatic code never writes it.

### 4.2 Destination (circuit breaker + operator pause)

```
            ┌──────────── success on any attempt ──────────────┐
            ▼                                                  │
   closed ── N consecutive transport failures ──▶ open ── cooldown ──▶ half_open
     ▲                                             ▲                     │
     │                                             │ probe failed        │ one probe batch
     └──────── probe succeeded ────────────────────┴─────────────────────┘  (rotating)

   any state ── hookspool pause ──▶ paused ── hookspool resume ──▶ closed (counters reset)
```

- **Transport failure** = connection refused/reset, timeout, 5xx other than 503-with-Retry-After, DNS error, script that cannot be started, script timeout. **Not** a transport failure: 4xx (config or permanent, batch-level), 429 or 503 with `Retry-After` (throttle), non-zero script exit (a batch-level retry, since the script ran).
- `trip_after` **consecutive transport failures on the destination** (default 3), whichever batches they came from → `open`; `cooldown_s` starts at `cooldown_initial_s` (30) and doubles on each re-trip up to `cooldown_max_s` (600). A destination fed by one key therefore trips on one batch's third failure, which is the point: the gate is per destination, not per batch.
- Only an attempt that **started after** `opened_at` (a probe) can re-trip or double the cooldown; attempts that were already in flight when the breaker opened record their outcome and nothing else.
- `open` → `half_open` when the cooldown elapses. In `half_open` exactly one batch is leased as a **probe**: the oldest due batch for the destination that is **not** `last_probe_batch_id` (if there is only one due batch, it is the probe). The claim writes `last_probe_batch_id` and `probe_until = lease_until`; while `probe_until > now` the tick claims nothing else for that destination. The attempt row has `probe = 1`, `counted = 0`.
- Probe ok → `closed`, `consecutive_failures = 0`, `cooldown_s` reset, `last_ok_at = now`, the delivery issue auto-resolves. Probe failed → `open` with the doubled cooldown; the probe's batch stays due and drains with the rest when the breaker closes (at the end of an ordinary outage the last failed probe's batch is always followed by a rotated probe on another batch, so one success elsewhere proves nothing about it). A batch is **poison** when it fails with a transport outcome while the breaker is `closed` and that failure completes a run of `POISON_STRIKES` (5) **strikes**: consecutive transport failures of this batch, each one after the destination accepted another batch since this batch's previous failure (a failed probe counts as a failure in the run; a lease that merely expired is skipped; any other outcome ends the run). It is marked `dead` with `last_class = poison`, the attempt is uncounted and does **not** count against the breaker, and a `delivery:<dest>:poison` issue names it, so the destination keeps draining. A whole outage scores no strike (nothing succeeds in between), and a receiver that fails at random with probability p kills a healthy batch with probability about p^6 (T9: the first rule, one failure after a failed probe and a success, killed about one healthy batch in four failed probes under a flaky receiver). The run does not need the breaker to trip, so a payload that crashes a receiver which keeps accepting the other batches is found too, instead of blocking its group for ever. If every probe fails whichever batch it carries, it is a real outage and the breaker stays open; the issue text names the last probe batch so an operator can `exclude`/`split` it if they suspect a poison payload on a single-batch destination.
- Trips and operator pauses open/update a `delivery` issue (section 11.4). Nothing is ever discarded in any state.

---

## 5. Coalescing semantics

Coalescing groups events per `(route, key)` into one batch and releases the batch when the burst has quietened, has gone on long enough, or is big enough. It is a debounce that keeps every event, in arrival order, bounded in size.

**Group**: `route` (resolved from source + match) and `key` (from the plugin). Keys are opaque strings, max 512 bytes.

**Tee copies.** A matching `tee = true` route (section 14.1) gets a copy of the event: an event row of its own, joined to the open batch of `(tee route, key)` by the same rule with that route's window values, so a copy counts against its own route's `max_bytes` and seals on its own route's quiet window. The primary copy and every tee copy of one request go through the join rule in the **same** enqueue transaction (primary first, then the tees in file order), so a crash leaves all of them or none, and the sender's 2xx follows that one COMMIT. Dedup runs once, before any copy is placed: the `dedup` row points at the primary copy, and a duplicate makes no copies. From there each copy is independent: its own lease, retries, breaker, dead state, `replay`, `exclude`, `split` and `drop`.

**Join rule** (enqueue transaction, `BEGIN IMMEDIATE`):

1. Find the batch for the group with `state = 'open'` (at most one).
2. If found and `bytes + size > max_bytes` and `count >= 1`: seal it (`state = 'sealed'`, `sealed_at = now`, `content_hash`) and treat it as not found. This keeps `max_bytes` a true bound: a document is never larger than `max_bytes` plus one event, and `check` ensures one event cannot exceed it (below).
3. If found: append the event, `last_at = max(last_at, now)`, `count += 1`, `bytes += size`.
4. Else: insert a new `open` batch with the route's window values copied in, then append.
5. If the batch now has `count >= max_events` or `bytes >= max_bytes`: seal it in the same transaction.

Because the lookup and the insert happen under `BEGIN IMMEDIATE`, two intake threads cannot open two batches for one group. `first_at`, `last_at` and `received_at` are facts and are never rewritten.

**Readiness** of an `open` batch at time `now` (the dispatcher's `now_eff`):

```
ready  =     quiet_s == 0 and max_s == 0            # immediate
          OR now >= last_at + quiet_s               # burst went quiet
          OR now >= first_at + max_s                # burst went on too long
```

Every dispatcher poll (`poll_s`, 1 s) first seals every ready `open` batch (`UPDATE batches SET state='sealed', sealed_at=now, content_hash=... WHERE state='open' AND ready`), **before** consulting any destination gate. Sealing is a property of the batch, not of the destination: a gated destination gets a queue of bounded, sealed batches instead of one growing document. Size limits seal in the enqueue transaction itself, so they hold even if the dispatcher is down.

Consequences to know: `immediate` still coalesces whatever arrives within one poll (up to 1 s); `quiet_s`/`max_s` overshoot by up to one poll. These are deliberate; a hard real-time cut would produce two batches a second apart for no gain.

**Window values** are inherited **per field**: for each of `quiet_s`, `max_s`, `max_events`, `max_bytes` the value comes from the route if set, else the source, else the `span` preset named by the source (presets set `quiet_s` and `max_s` only), else `[coalesce]` defaults (`quiet_s = 0`, `max_s = 0`, `max_events = 500`, `max_bytes = 786432`). `check` warns when `quiet_s > max_s` (the quiet window can never fire) and fails when the route's `max_bytes` exceeds three quarters of the destination's `max_body_bytes` or any feeding source's `max_body_bytes` exceeds the route's `max_bytes` (one event could overflow the document).

| preset | quiet_s | max_s | meant for |
|---|---|---|---|
| `burst` | 120 | 600 | code and CI (one push triggers many checks) |
| `mail` | 30 | 120 | mail, boards, tickets |
| `calendar` | 45 | 180 | calendar edits (one editing session = one pass) |
| `sync` | 60 | 300 | bank and data syncs that send several notifications per backfill |
| `immediate` | 0 | 0 | meetings, security, anything where latency matters more than batching |

**Ordering inside a batch** is `events.id` order, which is commit order. **Ordering across batches** of a group is FIFO (section 6.1).

**Replay** sets `not_before = now`; a batch with `not_before` in the future is not a candidate.

**Hermes' `coalesce` route option and `events` filter must be off** for routes fed by hookspool; `hookspool check` reads `subscriptions_path` and fails if either is set on a route a hookspool destination feeds. The exception is an `unwrap` destination (section 6.4): it sends original events one by one, which is what the gateway's filter and coalescing were written for, so `check` allows both there.

---

## 6. Delivery semantics

### 6.1 Ordering and parallelism

- The candidate query returns, per `(route, key)`, only the **oldest unfinished** batch (`state in ('open','sealed','retry','leased')`), oldest `first_at` first, and only if that batch is `sealed` (or `retry` with `next_attempt_at <= now`) and `not_before <= now`. A group whose head is leased, backing off or still open yields nothing. So delivery is strictly FIFO per group and parallel across groups.
- Global cap `dispatch.parallel` (default 4). Per-destination cap `max_inflight` (default 2 for `hermes`, 1 for `script`, 4 for `http`), counted in the dispatcher's memory.
- A `dead` or `dropped` batch is not "unfinished": it leaves the queue so the group proceeds. Replaying it later therefore delivers out of order; `hookspool replay` says so.

### 6.2 Leases and fencing

- Claim: `UPDATE batches SET state='leased', lease_until=?, lease_owner=? WHERE id=? AND state IN ('sealed','retry')` under `BEGIN IMMEDIATE`; `rowcount == 1` wins. `lease_until = now + destination.timeout_s + dispatch.lease_grace_s` (grace default 60).
- `lease_owner = "<hostname>:<pid>:<attempt_id>"` is a **fence**, not an audit field. Every `mark_delivered`, `mark_retry`, `mark_dead` runs `UPDATE batches SET ... WHERE id=? AND state='leased' AND lease_owner=?`; `rowcount == 0` means the worker lost its lease: the attempt row is completed with `outcome='discarded'`, the outcome (emits included, which is why they are in the same transaction) is not applied, and a warning is logged. Two deliveries of one batch can therefore overlap under a pathological clock, but only one of them can ever record a result.
- **Worker deadline**: a worker aborts its request (or kills the script's process group) at `lease_until - 5 s` whatever the per-operation `timeout_s` says, so a live owner never lets its own lease expire. The outcome is a transport failure (`deadline`).
- Recovery: at startup every `leased` row belongs to a dead process (the `flock` guarantees it) and goes to `retry`, due now, **uncounted**, `last_error = 'lease expired'`, its open attempt row finished with `outcome='retry'`. Every tick, the same is done for `leased` rows with `lease_until < now_eff` that no live worker of this process holds (an in-memory set). The dispatcher never recovers a lease one of its own live workers holds; the worker's deadline ends that attempt.

### 6.3 Destination gate

Before claiming, the dispatcher checks the destination row: `paused` or `open` → skip every batch for that destination this tick; `half_open` → claim at most one batch as a rotating probe, none while `probe_until > now`; `closed` → claim up to `max_inflight`. A stored `Retry-After` deadline (`retry_after_until`) is a soft gate that delays claiming without changing state.

### 6.4 Outbound idempotency headers

Every HTTP delivery carries:

```
webhook-id:          <delivery_id>
webhook-timestamp:   <unix seconds at send time>
webhook-signature:   v1,<base64(HMAC-SHA256(key, "<id>.<ts>.<body>"))>
X-Hub-Signature-256: sha256=<hex HMAC-SHA256(secret, body)>     (legacy receivers)
X-Hookspool-Batch:   <batch id>
X-Hookspool-Attempt: <attempt number>
Content-Type:        application/json
User-Agent:          hookspool/<version>
```

The signing key is the destination's secret: if it starts with `whsec_` the rest is base64-decoded, otherwise the raw UTF-8 bytes are used (both are what Hermes and Standard Webhooks receivers expect). Multiple signatures (space-separated) are emitted during a secret rotation when the destination lists `secret_previous`.

`delivery_id` is fixed by the destination **type** and its delivery mode (there is no per-destination id setting):

- `http`, `script`: **per batch**, `hs_<instance_id>_<batch_id>`, identical on every attempt, as Standard Webhooks intends. A receiver that keeps ids dedups a lease-expiry redelivery.
- `hermes` or `http` with `unwrap = true`: **per event**, `hs_<instance_id>_<batch_id>_<event id>`, identical on every attempt (section 6.4.1).
- `hermes`: **per attempt**, `hs_<instance_id>_<batch_id>_<attempt_id>`. Hermes records a delivery id *before* it runs a route and answers `200 status=duplicate` for an hour afterwards; `duplicate` is proof of admission, not of processing. With a per-batch id the sequence "Hermes admits, crashes before running, we see 502, we retry, Hermes says duplicate, we mark delivered" loses the batch. A fresh id per attempt keeps at-least-once true at the cost of a possible double wake-up after a dispatcher crash between the gateway's 200 and the mark. Routes that must not run twice dedup on `X-Hookspool-Batch` (stable per batch) on the Hermes side, where the admit-vs-process gap is theirs to close.

The document body (identical for Hermes, scripts and HTTP):

```json
{"source": "github", "route": "github-ci", "key": "owner/repo:ci", "batch_id": 123,
 "delivery_id": "hs_…", "attempt": 2, "count": 7, "first_at": 1.7e9, "last_at": 1.7e9,
 "event_type": "workflow_run", "event_types": {"check_suite": 5, "workflow_run": 2},
 "summary": "7 github event(s) for owner/repo:ci (check_suite x5, workflow_run x2)",
 "content_hash": "sha256:…",
 "events": [ {…payload…}, … ],
 "event_meta": [ {"id": 1, "event_type": "…", "received_at": 1.7e9, "dedup_id": "…"}, … ]}
```

`events` and `event_meta` are frozen at seal and are what `content_hash` covers; `delivery_id`, `attempt` and the headers change per attempt by design. `event_type` is the last event's type and `event_types` counts every type in the batch (so a receiver can see mixed batches instead of guessing from one field); a source's `event_header` (the GitHub example sets `X-GitHub-Event`) carries `event_type` as a header. `summary` is a plain count line, never an interpretation. Excluded events are omitted from both arrays.

#### 6.4.1 Unwrap: one request per event

Some receivers expect the sender's original payload, one request per event, not the document: a Hermes route whose prompt template reads `{pull_request.title}`, or an endpoint written for the sender itself. A `hermes` or `http` destination with `unwrap = true` (default `false`) keeps every promise above (commit before ack, retry until accepted, the breaker, issues) and changes only the wire:

- **Requests.** A batch is delivered as one POST per event, in `events.id` order. The body is the event's stored payload, byte for byte as the spool holds it (`events.payload`), never the document and never a re-encoding. The dispatcher reads those bytes with the document and hands them to the destination in `ctx.payloads`; the destination's `max_body_bytes` then applies to each event, not to the document.
- **Headers.** The Standard Webhooks headers with a per-event `webhook-id` `hs_<instance_id>_<batch_id>_<event id>`, the same on every attempt; `X-Hub-Signature-256` over that body (the route secret for `hermes`, the current `secret` for `http`); `X-Hookspool-Batch` and `X-Hookspool-Attempt` as usual. `event_header` (a destination option; a `hermes` destination without it falls back to the source's `event_header`) carries the event's `event_type`, and `dedup_header` carries its `dedup_id` when it has one (the GitHub case: `X-GitHub-Event`, `X-GitHub-Delivery`). Both are refused by `check` when they name a header hookspool sets, and a value that cannot go on the wire is left out.
- **Outcome.** The batch is `ok` only when every event's request was (2xx; for Hermes `delivered`, `accepted`, `duplicate`, and also `ignored` and `coalesced`). On the first failure the attempt stops and its outcome is classified exactly as a document's would be (transport, throttle, config, permanent; section 6.5), so the whole batch retries, trips the breaker or goes dead as usual. The retry starts again at the first event: events the receiver already accepted are re-sent with the same id and answered `duplicate` by a receiver that keeps ids, and no event is ever sent before an earlier one of its batch has been accepted. The attempt row records `events_delivered` and `failed_event_id`; reasons and details carry ids and counts, never a body.
- **Idempotency trade-off.** A per-event id is stable, so the admit-vs-process gap described above for Hermes is the receiver's again: a gateway that admits an event, then crashes before running it, will answer `duplicate` to the retry. That is the price of letting the receiver's duplicate detection make a replay safe, which is what a route reading original events needs; use the document mode where a lost wake-up is worse than a double one. Hermes answers `duplicate` only within its one-hour window, so a `replay`, or a backoff that outlasts the hour, sends the already-accepted events again as new, and the route runs them again.
- **Hermes answers.** `ignored` (the route's own `events` filter) and `coalesced` (its own `coalesce`) are expected when the gateway sees original events, so they are `ok` without a notice or an issue. `check` does not fail on a route's `events` or `coalesce` when its destination unwraps (section 5).
- **Coalescing.** Unwrap is meant for routes with coalescing off (`quiet_s = max_s = 0`, the `immediate` span), so each event is its own batch. With a window, every event of the batch is still delivered, in order, and the receiver gets them as a burst; `check` warns but does not fail. All the requests of one attempt share the attempt's deadline (section 6.2); a batch that runs out of time resumes on the next attempt, its accepted events answered `duplicate`.

### 6.5 Retry, backoff and the attempt counter

| outcome | class | examples | batch | counted | breaker |
|---|---|---|---|---|---|
| `ok` | — | 2xx; exit 0; Hermes `status` delivered, accepted, duplicate; also `ignored`, `coalesced` (which open a `config` issue, section 11.4) | `delivered` | yes | success |
| `retry` | `transport` | refused, reset, timeout, worker deadline, DNS, 5xx, cannot start script, script timeout | `retry`, backoff | **no** | failure |
| `retry` | `throttle` | 429, 503 with `Retry-After` | `retry` at `Retry-After` (else backoff) | no | neutral |
| `retry` | `batch` | non-zero script exit other than 65 (a processor's retry exit is 75), bad emit | `retry`, backoff | **yes** | neutral |
| `retry` | `config` | 401, 403, 404, 405, 410 from a destination; unknown destination, script or route; `subscriptions_path`, secret or env file unreadable | `retry` at the backoff cap; due again at once after a successful config reload | no | neutral; `config` issue |
| `dead` | `permanent` | 400, 413, 415, 422 (the document itself is rejected, which no config change alters); 3xx on `http`; script exit 65 (a processor's `Permanent`); emit validation failed twice | `dead` + issue + alert | — | neutral |
| `dead` | `poison` | the repeated-strike rule of section 4.2 | `dead` + issue + alert | — | closes |

The counter exists for one case: a destination that is **responsive but fails on this batch** (`batch` class). Backoff after the n-th counted failure: `min(cap, base * 2^(n-1)) + jitter`, base 15 s, cap 300 s, jitter uniform 0–25 %; uncounted retries use the same schedule keyed on `consecutive_failures` of the destination. `max_attempts` default 64 (a script failing on one batch for about five hours, with other batches flowing). `Retry-After` overrides backoff when larger, capped at 1 h. A dead batch opens an issue whose text includes the `hookspool replay <id>` command.

`config` outcomes are retried uncounted because they are the operator's to fix and often transient: a 401 during a secret rotation, a 404 while the intake has reloaded a route before the dispatcher reloaded its destination. The batch stays at the head of its group. When the dispatcher applies a config reload successfully, every `retry` batch whose `last_class = 'config'` is made due now, so a fix or a reload race costs seconds, not the backoff cap.

### 6.6 Script destinations and durable emits

A `script:<name>` destination runs `argv` with the batch document on stdin and `HOOKSPOOL_BATCH_ID`, `HOOKSPOOL_DELIVERY_ID` in the environment, in its own session (so a timeout kills the whole process group). Exit 0 is success; exit 65 (a processor's `Permanent`) dead-letters the batch at once and every other non-zero exit is retried. Stdout lines of the form `{"enqueue": {...}}` are validated (same rules as the intake) and inserted **in the same fenced transaction** that marks the batch delivered: either the script's effects are recorded and the batch is done, or neither. A script that emits nothing and exits 0 is a successful no-op. Emits may target any route (the usual case: a bobbin's processor or a delta-style script emits a `hermes:` route event when something needs the agent).

Scripts therefore never talk to the gateway themselves; they report facts to the spool and the spool delivers them. Scripts that keep their own state (history ids, sync tokens) write it under a directory the deployment lists in the unit's `ReadWritePaths` (section 13 and `deploy/README.md`).

A script that advances its own state while it reads has one more hazard: killed between "state advanced" and "emits committed" (or losing its lease after exiting 0), it would lose its report, because the retry finds nothing new. The processor runtime (14.3.4) and any script that imports `deploy/scripts/hookspool_batch.py` therefore stage their state writes, write the emits and the staged writes to a journal in `state_dir` keyed by `HOOKSPOOL_BATCH_ID` (stable across attempts for a `script`), apply the state, then print the emits; a retry of the same batch replays the journal and does not read the outside system again (`deploy/scripts/README.md`). Any script with that shape should do the same.

---

## 7. Dedup windows

- A source plugin returns `dedup_id(payload, req) -> str | None`. `None` means "no dedup for this event". Built-ins for plugins: `dedup.body_sha256`, `dedup.header("X-GitHub-Delivery")`, `dedup.field("messageId")`.
- The intake inserts `(source, dedup_id)` into `dedup` inside the enqueue transaction. A conflict is a duplicate: `dup_count += 1`, the response is 200 with `{"status":"duplicate"}`, nothing else changes. The response is 2xx on purpose: the sender wanted an ack, and it already has the event.
- Exception: if the original event's batch is `dead`, the hit is treated as the operator asking for a retry (a provider-dashboard redelivery inside the window): the batch is replayed and the response is `200 {"status":"replayed"}`. If the batch is `dropped`, the operator chose to discard it; the redelivery is enqueued as a new event and the dedup row is re-pointed (`200 queued`).
- Window: `dedup_window_s` per source, default 86 400; the effective window is **at least** that (the hourly prune makes it up to an hour longer). Retention deletes `dedup` rows older than the window, which reopens the id. A redelivery from the provider's dashboard after the window is delivered again; that is the operator asking for it. `check` warns when a source's window exceeds `retention.delivered_days`.
- Dedup is by id, never by content, unless the plugin chooses `body_sha256` (Plaid, whose payloads carry no id; its window is 600 s so a legitimately identical body a day later is a new event).
- Which sources have a real id: GitHub `X-GitHub-Delivery`; Trello action id; Zoom `event_ts` + object id; Google Calendar `X-Goog-Message-Number` per channel; Kaneo its event id if present. Gmail's Pub/Sub `messageId` is per **push**, not per mailbox change: after "intake killed after COMMIT" a Pub/Sub retry carries a new `messageId` and becomes a second event in the batch, which the processor reconciles.
- Receivers must still be idempotent: dedup is a cost saver, not a guarantee (the Hookdeck caveat applies here too).

---

## 8. Retention

Runs in the dispatcher's maintenance tick, once an hour, in small transactions (1 000 rows each) so the intake never waits long.

| data | default | setting |
|---|---|---|
| `delivered` batches and their events, raw bodies | 7 days after `delivered_at` | `retention.delivered_days` |
| `attempts` | 30 days | `retention.attempts_days` |
| `dropped` batches | 7 days | `retention.dropped_days` |
| `dead` batches | **never** | — (operator replays or drops; `status` shows the count; the issue stays open) |
| `dedup` rows | the source's `dedup_window_s` | per source |
| `issues` resolved/ignored | 90 days | `retention.issues_days` |

After a pass: `PRAGMA wal_checkpoint(TRUNCATE)`, and `VACUUM` only when `hookspool prune --vacuum` is run by hand (it needs 2× the file size free). Backup is `sqlite3 spool.db "VACUUM INTO 'backup.db'"` (consistent, online) and `hookspool export --state delivered --since T` for a JSONL copy; there is no built-in archive.

---

## 9. Intake, backpressure and quarantine

**Request order** (section 10 for the reasoning): body cap → `verify` → `challenge` → `ignore` → `payload` → `key`, `dedup_id`, `event_type`, `attrs` → route resolution (the primary route and any tee routes) → `enqueue` of every copy in one transaction → response.

**Body cap**: `Content-Length` is required; `Transfer-Encoding: chunked` is answered 411 (the stdlib server does not decode it; the reverse proxy buffers requests, see `deploy/nginx.example.conf`). A body longer than `max_body_bytes` is 413 before any plugin code runs.

**Plugin errors after verification are quarantined, never 5xx'd.** If `ignore`, `payload`, `key`, `dedup_id`, `event_type`, `attrs` or route resolution raises, the intake writes the raw body and the request headers (minus the source's `auth_headers`) as an event in a **quarantine batch** (`route = <source>`, `key = "quarantine"`, `destination = "quarantine"`, state `dead`, `last_class = quarantine`, `last_error = "plugin error: <ExceptionClass>"`), commits, answers `202 {"status":"quarantined"}`, and opens a `config:plugin:<source>` issue. No delivery is attempted. `hookspool replay <batch>` on a quarantine batch re-runs classification through the (fixed) plugin: each stored request becomes a normal event in a normal batch, and the quarantine batch is marked `delivered` with `result = "reclassified"`. Nothing verified is ever answered with a 5xx because of our own code; the only 5xx the intake sends is 503 when the spool cannot take the write.

**Backpressure**: when the spool cannot keep a request the intake says so instead of accepting what it could not keep.

| condition | check | intake response | dispatcher |
|---|---|---|---|
| free space on the state filesystem < `limits.min_free_bytes` (512 MiB) | `os.statvfs`, cached 5 s | 503, `Retry-After: 60` | keeps delivering (it frees space); alert + `spool` issue |
| `SQLITE_FULL` / `OSError ENOSPC` during enqueue | the write fails | 503 | alert |
| `SQLITE_BUSY` beyond `busy_timeout` | the write fails | 503 | retries next tick |
| body > source `max_body_bytes` (1 MiB) | `Content-Length` and a hard read cap | 413 | — |
| chunked request | `Transfer-Encoding` header | 411 | — |
| request rate | not hookspool's job | the reverse proxy's `limit_req` answers 429/503 | — |

A 2xx is only ever sent after `COMMIT` returned. `/healthz` reports `degraded` with the reason while a limit is active.

**Which senders retry a 5xx** (this matters: the "sender holds it" guarantee is only as good as the sender):

| sender | on non-2xx | consequence for hookspool |
|---|---|---|
| GitHub | **no automatic retry**; redelivery is manual (UI or `POST /repos/{o}/{r}/hooks/{id}/deliveries/{id}/attempts`) | a 503 is a loss until the sweep redelivers it; the GitHub sweep in section 13 lists failed deliveries and redelivers them |
| Zoom | retries a few times over a few minutes | short outages are covered; the Zoom sweep covers the rest |
| Trello | retries a few times; a webhook that keeps failing can be disabled by Trello | the Trello sweep re-creates webhooks it finds missing |
| Google Pub/Sub (gmail) | retries with backoff for the subscription's retention (days) | covered |
| Google Calendar push | retries with backoff for a limited time | mostly covered; the `expect_every_s` sweep re-watches the channel |
| Plaid | retries with backoff over hours | covered |
| Kaneo and other sources | check the provider; assume **no retry** until shown otherwise | the source's sweep must reconcile |

Quarantine exists because of this table: with GitHub, a plugin bug that answered 500 would lose every event until someone noticed.

---

## 10. Security

- **Body cap, then verification, then everything else.** The intake reads at most `max_body_bytes`; a longer body is 413 before any plugin code runs. Then `verify()`; only a verified request reaches `challenge`, `ignore`, `key`, `dedup_id`, enqueue. An unverified request gets `401` with an empty body and a single log line with source, remote address and size; nothing else.
- **Handshakes are verified like everything else.** Zoom signs its `endpoint.url_validation` request and GitHub signs `ping`, so they are answered only after `verify()` succeeds. This matters because a handshake answer can be derived from the secret: Zoom's `encryptedToken` is `HMAC-SHA256(webhook_secret, plainToken)` with an attacker-chosen `plainToken`, and the same secret signs Zoom's events, so answering it unauthenticated is a signing oracle for forged events. A source whose handshake genuinely carries no signature (Trello's `HEAD`, which has no body and expects an empty 200) declares `challenge_unauthenticated = True`; then `challenge()` runs before `verify()` but may only return a constant response (the test kit checks that it does not read `secrets`). `sourcekit` includes "handshake request with a bad signature → 401". GitHub `ping` is an `ignore()` case, not a handshake: it is verified, acked with `{"status":"ignored"}`, not enqueued.
- **Handshakes never enqueue.** `challenge()` returns a response and stops; the handler cannot fall through to enqueue.
- **Constant-time compares everywhere**: `hookspool.verify` wraps `hmac.compare_digest` and is the only comparison API plugins are given (`hmac_hex`, `hmac_b64`, `hmac_prefixed`, `timestamped` (`t=…,v1=…` schemes), `standard_webhooks`, `static_token`, `jwt_es256` via PyJWT). Every timestamped scheme takes `tolerance_s` (default 300) and the check is two-sided, `abs(now - t) <= tolerance_s`: future-dated requests are rejected too, and the test vectors include one.
- **Replay windows differ per scheme.** GitHub's scheme has no timestamp, so a captured legitimate delivery can be replayed after the dedup window (TLS termination at the proxy makes capture unlikely, and the dedup window makes it a duplicate for 24 h). Zoom, Standard Webhooks and Plaid (`iat`) carry timestamps. hookspool adds no replay check of its own that would reject a legitimate provider retry.
- **Plaid key fetches are guarded.** The verifier checks the JWT's structure and `alg == ES256` before touching the network, caches known keys, negative-caches unknown `kid`s for 10 min, and allows at most one key fetch per minute per source, so unauthenticated requests with random `kid`s cannot amplify against Plaid's API or tie up intake threads.
- **Secrets are references, not values.** Config holds `{file = "…"}`, `{env = "NAME"}` or `{env_file = "…", key = "NAME"}`; values are read at use time, never cached across requests, never logged, never written to the database. `file` and `env_file` refs pick up a rotation without a restart; `env` refs do not (a process's environment does not change under it) and `check` says so. Files are checked for mode `0o600`-ish (group/other readable → warning in `hookspool check`).
- **No secrets, bodies or headers in logs.** Log records carry ids, names, sizes, states, codes and durations. The test suite plants a sentinel in every payload and secret and fails if any log line contains it. Query strings are never logged (Pub/Sub tokens live there).
- **Headers stored** are only those a source lists in `keep_headers`; every source also declares `auth_headers` (signature, token, authorization headers), which are refused from `keep_headers` at config check and stripped before a quarantined request is stored.
- **Loopback only.** The intake binds `127.0.0.1` (or a Unix socket). It trusts `X-Forwarded-For` only from loopback and only for logging. The reverse proxy owns TLS, the hostname, request buffering, rate limiting and the path → `X-Hookspool-Route` mapping used to keep provider-side URLs stable.
- **Outbound**: destinations are configuration, not payload data, so there is no SSRF surface; `http` destinations require `https://` unless `allow_insecure = true`; TLS verification is on; redirects are not followed (a 3xx is a permanent failure, per Standard Webhooks).
- **Scripts** run with a minimal environment (`PATH`, `HOME`, `LANG`, the env file, the two `HOOKSPOOL_*` variables), `start_new_session=True`, a timeout, and stdout capped at 1 MiB.
- **Database file** mode `0o600`, directory `0o700`.
- **Later hardening, noted, not done now**: Pub/Sub push can carry an OIDC `Authorization` header verifiable against Google's certificates with the libraries already present; the static URL token is the weakest verifier in the set.

---

## 11. Observability

### 11.1 `hookspool status` and `GET /healthz`

Both produce the same JSON:

```json
{"ok": true, "degraded": [], "schema": 2,
 "dispatcher": {"alive": true, "heartbeat_age_s": 4.0, "pid": 4242},
 "spool": {"last_enqueue_age_s": 12.0, "writable_checked_age_s": 20.0},
 "batches": {"open": 3, "sealed": 1, "retry": 0, "leased": 1, "delivered": 1412, "dead": 0, "dropped": 0, "quarantined": 0},
 "oldest_pending_age_s": 41.0,
 "destinations": {"hermes:github-ci": {"state": "closed", "inflight": 1, "oldest_pending_age_s": 41.0},
                  "script:gmail_delta": {"state": "open", "opened_at": 1.7e9, "cooldown_s": 600, "oldest_pending_age_s": 259000.0, "last_error": "connection refused"}},
 "sources": {"github": {"last_event_age_s": 12.0}, "gcal": {"last_event_age_s": 90000.0, "expected_every_s": 86400, "silent": true}},
 "issues_open": 1, "spool_bytes": 10485760, "disk_free_bytes": 40000000000}
```

`/healthz` is 200 when `ok`, 503 when the spool is unwritable or a hard limit is active, and 200 with `degraded` listing soft problems (dead or quarantined batches, open breaker, silent source, dispatcher heartbeat older than 30 s). Writability is proven by the last successful enqueue when one happened in the last 30 s, otherwise by one tiny write at most every 30 s; a scrape never forces an fsync by itself. Per-destination `oldest_pending_age_s`, `opened_at` and `cooldown_s` make "degraded" actionable.

### 11.2 Metrics

`GET /metrics` on the intake, Prometheus text format, no dependency, nothing persisted for it.

- Intake counters are per process in memory and reset on restart, which Prometheus `rate()` handles: `hookspool_events_received_total{source}`, `hookspool_events_duplicate_total{source}`, `hookspool_events_quarantined_total{source}`, `hookspool_requests_rejected_total{source,reason}` (`unverified`, `too_large`, `chunked`, `spool_full`, `busy`, `ignored`).
- Dispatcher figures are computed from the tables at scrape time and exposed as **gauges over the retention window**, because a counter computed from a pruned table would not be monotonic: `hookspool_attempts_30d{destination,outcome,klass}`, `hookspool_batches_delivered_7d{destination}`, `hookspool_attempt_duration_seconds_sum_30d/count_30d{destination}`.
- Gauges: `hookspool_batches{state}`, `hookspool_oldest_pending_age_seconds`, `hookspool_destination_state{destination}` (0 closed, 1 half_open, 2 open, 3 paused), `hookspool_destination_oldest_pending_age_seconds{destination}`, `hookspool_issues_open`, `hookspool_spool_bytes`, `hookspool_disk_free_bytes`, `hookspool_dispatcher_heartbeat_age_seconds`, `hookspool_source_last_event_age_seconds{source}`.

### 11.3 Structured logs

stdlib `logging` to stderr (journald captures it). `HOOKSPOOL_LOG=json` emits one JSON object per line with fixed fields: `ts, level, component, msg, source, route, batch, event, destination, attempt, status, bytes, duration_ms, reason, tee`. The intake writes one `queued` line per copy of a request; a tee copy's line carries `tee: true`. `text` is the human form of the same fields. There is no debug level that prints bodies.

### 11.4 Issues

An issue is a grouped failure with a fingerprint:

| kind | fingerprint | opened by | auto-resolved by |
|---|---|---|---|
| `delivery` | `delivery:<destination>:<reason class>` (reason class: `unreachable`, `http_5xx`, `timeout`, `script_exit`, `batch` (a counted failure on a non-script destination), `exhausted` (a batch `dead` at `max_attempts`), `permanent`, `poison`, `ignored`) | a batch going `dead`, a breaker trip, a counted failure streak ≥ 3, a Hermes `ignored`/`coalesced` answer | a successful attempt on that destination (for `permanent`/`poison`/`exhausted`: only when the dead batches for it are replayed or dropped; for `ignored`: operator `resolve`) |
| `source_silent` | `source_silent:<source>` | no event for > `expect_every_s` | the next event |
| `spool` | `spool:<limit>` | a backpressure limit engaged | the limit clearing |
| `config` | `config:file` (a reload that failed validation), `config:plugin:<source>` (quarantine), `config:delivery:<destination>` (a `config`-class outcome) | the failing check | a reload that succeeded / the quarantine batches reclassified or dropped / a successful attempt |

State `open → acked → resolved`, or `ignored` (never alerts or reopens; `hookspool issues ignore <id>`; use sparingly, as Hookdeck says). Each open issue alerts **once** (`alerted_at`), then again only when it reopens after a resolve; the `alerts` rate limit (1 per 30 min per kind) still applies on top. Alerts run `alerts.argv` with the text as the last argument **from a worker thread**, never the main loop, and `alerted_at` is set only when the command exits 0, so an alert is not lost because the chat sender was down. `alerts.dry = true` (or `HOOKSPOOL_ALERT_DRY=1`) records instead of sending.

---

## 12. Failure modes

| failure | what happens | guarantee |
|---|---|---|
| Intake killed before `COMMIT` | No row; no 2xx was sent. The proxy returns 502 or the connection drops. | no loss **if the sender retries** (section 9 says which do); the source's sweep covers the rest |
| Intake killed after `COMMIT`, before the response | Event is spooled. Sender sees no 2xx and retries; the retry is a dedup hit (`200 duplicate`) for sources with a real id, or a second event in the same batch (gmail, sources without ids). | no loss; possible duplicate, coalesced |
| Dispatcher killed after claiming, before the request | Startup recovery → `retry` due now, uncounted; the attempt row says `lease expired`. | at-least-once |
| Dispatcher killed after the destination's 200, before the mark | Same; the batch is delivered again with the same events and `content_hash` (and the same `webhook-id` for `http`/`script`, so the receiver dedups; a fresh id for Hermes, so a sweep route may run twice). | at-least-once |
| Dispatcher killed between a script's exit and the mark | The script runs again. Emits from the first run were never committed (same transaction as the mark), so nothing is duplicated in the spool; the script's own external side effects must be idempotent, which the script contract says. | at-least-once, atomic emits |
| Destination down for days | 3 transport failures → breaker `open`; one rotating probe per cooldown (30 s → 10 min); transport failures are never counted, so `max_attempts` cannot fire; events keep spooling in sealed, bounded batches; one issue, one alert. On recovery the probe succeeds, the breaker closes, batches drain FIFO per key at `max_inflight`. | no loss, no drop, no batch exceeds its bounds |
| Destination fed by one key is down | Same as above: the breaker trips on the third failure of the one batch; the gate is per destination. | no loss |
| Slow attempt outlives its lease | The worker aborts at `lease_until - 5 s`; if it somehow returns later, its mark is fenced by `lease_owner` and discarded. | at most one recorded result per attempt |
| Destination returns 401/403/404 | `config` retry, uncounted, issue `config:delivery:<dest>`; the batch keeps its place; a successful reload makes it due at once. | no loss, operator fixes config |
| Destination returns 413/415/422/400 | Batch `dead` (`permanent`), issue + alert with the replay command; the operator `split`s or `exclude`s and replays; other keys unaffected; the same key continues with its next batch. 413 should not happen: `check` bounds `max_bytes` by the destination's cap. | operator decides |
| Plugin raises on a verified request | Quarantine batch, `202 quarantined`, `config:plugin:<source>` issue; `replay` reclassifies after the fix. | no loss, no 5xx |
| `SQLITE_BUSY` | Writers wait `busy_timeout`; the intake returns 503 after 10 s; the dispatcher logs and retries next tick. Transactions are milliseconds, so this only happens under a stuck checkpoint. | no loss if the sender retries |
| Disk full / `ENOSPC` | Enqueue fails → 503. The dispatcher keeps delivering (reads and tiny writes may still succeed after retention frees space); `spool` issue + alert. | no loss if the sender retries |
| DB corruption detected by `quick_check` | Both processes refuse to start: the dispatcher checks at startup, and `hookspool check` (both units' `ExecStartPre`) checks for the intake. The failed units are the signal (the host's service monitoring, and `/healthz` stops answering); no `alerts.argv` command runs, since the dispatcher that sends alerts is the process that refused. Never "move aside and start fresh" automatically. Recovery is `hookspool export` from the readable parts and a restore from backup. | no silent loss |
| Clock jumps backwards (NTP step) | `now_eff = max(wall, last_tick_at)`: the dispatcher's time stands still until the wall clock catches up; no row is touched. | bounded delay, facts unchanged |
| Clock jumps forward | Every window closes at once; worker deadlines fire early (transport retry); leases of this process's live workers are never recovered by the tick, and any late mark is fenced. | at-least-once |
| Duplicate deliveries from the provider | `dedup_id` hit inside the window → `200 duplicate`; on a `dead` batch → replay; outside the window → delivered again (intended). | coalesced |
| Poison payload (crashes the destination) | Its transport failures retry uncounted (and may trip the breaker; the probe rotates). When it has failed six times in a row with the destination accepting other batches between its attempts (breaker `closed`), it is `dead` (`poison`) with an issue and the destination drains (six crashes of the receiver in all, about a minute and a half at the base backoff). On a single-batch destination the issue names the probe batch for `exclude`/`split`. | contained |
| Script hangs | Timeout → kill the process group → `retry` (transport) → breaker for that script. | contained |
| Two dispatchers started | The second fails the `flock` and exits 3 (systemd shows it). | single writer |
| Config reload with errors | Previous config stays active; `config:file` issue. The intake rejects a start with a bad config (`hookspool check` runs at `ExecStartPre`). | fail safe |
| Intake and dispatcher reload at different moments | A route to a destination the dispatcher does not know yet is a `config` retry, made due as soon as the dispatcher reloads (within one tick). | seconds of delay |
| Secrets or env file unreadable at delivery | `config` retry with an issue; not `dead`, because the file is the operator's. | no loss |
| Very large body | 413 before plugin code; a source that legitimately needs more raises `max_body_bytes` (and `check` makes it raise the route's `max_bytes` and the destination's cap with it). | bounded memory |
| Dedup table grows (no retention for a quiet source) | Rows are pruned by `seen_at`, independent of events. | bounded |
| Reverse proxy down | Senders retry or do not (section 9); hookspool is idle. | provider-side |

---

## 13. The maintenance-sweep convention

A webhook replaces a poll, but never fully: channels expire, subscriptions get dropped after failures, providers lose deliveries, and some providers do not retry. Every migrated job keeps a **sweep** that is cheap, rare, and idempotent:

1. The cron job that used to poll keeps running at 1–2 runs/day (or is deleted if the push covers it completely and the source has `expect_every_s`).
2. The sweep must be idempotent with the push path (it reconciles state; it does not re-announce what the push already delivered).
3. The source declares `expect_every_s`; if no event arrives within it, a `source_silent` issue opens, which is the signal that the push path is broken and the sweep is carrying the load.
4. A sweep may itself go through hookspool (`hookspool enqueue --source sweep …` or a `script:` emit), so its deliveries get the same coalescing and audit trail.
5. For a sender that does not retry (GitHub), the sweep also **redelivers failed deliveries**: `gh api repos/{o}/{r}/hooks/{id}/deliveries` filtered to non-2xx, then `POST …/deliveries/{id}/attempts`; dedup makes the ones that did land harmless.

`docs/ADDING_A_SOURCE.md` requires each source to state its sweep (job, cadence, `expect_every_s`), whether the sender retries, and the paths its scripts write (for `ReadWritePaths`), or say why none is needed.

---

## 14. Plugin API

### 14.1 Sources

A source is a Python module (built in under `hookspool/sources/<name>.py`, or any importable module named by `module = "pkg.mod"` in config) that defines these names. Only `verify`, `key` and `auth_headers` are mandatory.

```python
from hookspool.plugin import Request, Response, Secrets, verify as v, dedup

name = "github"                                  # default source name
secrets = ("secret",)                            # secret refs the source needs
auth_headers = ("X-Hub-Signature-256",)          # never stored, refused from keep_headers
keep_headers = ("X-GitHub-Event", "X-GitHub-Delivery")
span = "burst"                                   # default coalesce preset (route/source config overrides)
expect_every_s = None                            # optional liveness expectation
sender_retries = False                           # documented honesty; shown by `check` and in the sweep table
challenge_unauthenticated = False                # True only for a constant, secret-free handshake (Trello HEAD)

def verify(req: Request, secrets: Secrets) -> bool:
    return v.hmac_prefixed(req.header("X-Hub-Signature-256"), "sha256=", secrets["secret"], req.body)

def challenge(req: Request, secrets: Secrets) -> Response | None:  # optional; runs AFTER verify
    ...                                           # return a Response to answer and stop, None to continue

def ignore(req: Request) -> str | None:           # optional; a reason string means "ack, do not enqueue"
    return "ping" if req.header("X-GitHub-Event") == "ping" else None

def payload(req: Request) -> dict:                # optional; default: req.json() or {} ; gcal builds one from headers
    ...

def key(payload: dict, req: Request) -> str:
    return f"{payload['repository']['full_name']}:{classify(req.header('X-GitHub-Event'))}"

def dedup_id(payload: dict, req: Request) -> str | None:   # optional; default None
    return req.header("X-GitHub-Delivery") or None

def event_type(payload: dict, req: Request) -> str:         # optional; default ""
    return req.header("X-GitHub-Event")

def attrs(payload: dict, req: Request) -> dict:             # optional; keys for route `match`
    return {"repo": payload["repository"]["full_name"], "class": classify(...)}

action = "hermes:github"                          # default destination; a callable (payload, req) -> str is allowed
```

`Request`: `method`, `path`, `query` (dict), `header(name)` (case-insensitive), `body` (bytes), `json()` (cached, raises `BadPayload`), `route_hint` (from `X-Hookspool-Route`, set by the proxy), `remote_addr`, `now`. `Response(status, body=b"", headers={})`. `Secrets` is a lazy mapping that resolves refs at access time; for a `challenge_unauthenticated` source the `challenge()` call receives a `Secrets` that raises on access.

The intake calls, in order: body cap → `verify` → `challenge` → `ignore` → `payload` → `key`, `dedup_id`, `event_type`, `attrs` → route resolution → `enqueue`; an exception anywhere after `verify` quarantines the request (section 9). The shared test kit (`tests/sourcekit.py`) exercises every plugin the same way (a static-token source such as Gmail or Calendar sets `Kit.body_signed = False`, which skips the tampered-body case because the token does not cover the body): signed, unsigned, tampered, future-dated and stale timestamps, oversized, chunked, duplicate, handshake with a good and a bad signature, constant-ness of an unauthenticated handshake, plugin exception → quarantine, spool-unwritable → 503, and the sentinel-in-logs check.

Route resolution: config `[[routes]]` entries are evaluated in order; the first whose `source` matches and whose `match` table is a subset of `attrs` wins; its `destination` overrides the plugin's `action`. With no matching route, the implicit route `<source>` uses the plugin's `action` and the source's window.

A route with `tee = true` is skipped by that first-match search: when it matches it receives a copy of the event and does not claim it, so the event still goes to the first matching non-tee route, or to the implicit route. Adding a tee route never changes where an event already went (one event to two places is the Svix and Convoy "several subscriptions on one event type", the Hookdeck "several connections on one source", as a flag on a route). Every matching tee route gets one copy, in file order, at most 8 per event. `check` refuses a tee route without a `destination`, a tee destination equal to the source's `action` or to any non-tee route's destination on the same source, one equal to an earlier tee route's destination whose `match` can overlap, and more than 8 tee routes on a source, so one event never reaches one destination twice; the intake also skips a tee whose destination already has a copy of the event. Copies are placed as section 5 describes. A quarantined request that `replay` reclassifies makes the same copies.

### 14.2 Destinations

A destination type is a class registered under `hookspool.destinations`. Built in: `hermes`, `script`, `http`. Others: `module = "pkg.mod:ClassName"`.

Round-1 notes: a `hermes` destination adds the event type as a header named by the source's `event_header` setting (the GitHub example sets `X-GitHub-Event`; a name hookspool sets itself is refused by `check`, and a value that cannot go on the wire is left out). A script's emit line that opens as `{"enqueue"` (any spacing or quoting) but is invalid JSON or UTF-8 fails the attempt as `bad emit`, and the dispatcher applies the same "twice, then permanent" rule to it as to a store-rejected emit. Scripts get only `PATH`, `HOME` and `LANG` from the dispatcher's environment.

```python
class Destination(Protocol):
    name: str                                     # "hermes:github-ci"
    timeout_s: float
    max_body_bytes: int                           # what the receiver accepts; `check` bounds max_bytes by it
    idempotency: Literal["per-batch", "per-attempt", "per-event"]   # fixed by the type ("per-event": unwrap)
    unwrap: bool                                  # one request per event; ctx.payloads holds the stored bodies
    def deliver(self, doc: dict, body: bytes, delivery_id: str, ctx: DeliveryContext) -> Outcome: ...
    def check(self) -> list[str]:                 # config/secret problems for `hookspool check`
```

`ctx.deadline` is the worker deadline (`lease_until - 5 s`); `deliver` must not outlive it. `Outcome(kind: "ok"|"retry"|"dead", klass: "transport"|"throttle"|"batch"|"config"|"permanent"|None, status: int|None, reason: str, detail: str, retry_after_s: float|None, emits: list[dict], notice: str|None, events_delivered: int|None, failed_event_id: int|None)`; the last two are an unwrap delivery's per-event progress, stored on the attempt row. `reason` is safe for logs and alerts; `detail` is stored in `last_error` (scrubbed to 500 chars) and never logged; `notice` (for example Hermes `ignored`) opens an issue without changing the outcome.

- **`hermes`**: POSTs the document to `<base_url>/p/<profile>/webhooks/<route>` (or `/webhooks/<route>` without a profile), with the headers of section 6.4. The route secret and profile are read fresh from `subscriptions_path` on each attempt (rotation without restart). 2xx is `ok`; Hermes `status` `ignored` or `coalesced` adds a `notice`. 401, 403, 404 are `config` with pointed reasons (`bad secret`, `unknown route`); 413 is `permanent`. `max_body_bytes` defaults to 1 MiB, Hermes' own default (`max_body_bytes` in its webhook platform config). Idempotency per attempt, `max_inflight = 2`.
- **Status mapping shared by `hermes` and `http`** (section 6.5 is the table): any 4xx not named there (409, 418, ...) is `config`, because it needs a person and must never be dropped; 408 is `transport`; a 3xx is never followed and is `permanent` on `http` but `config` on `hermes` (the base URL is wrong, not the document); a TLS certificate failure is `config`. Every request is bounded in total time by `min(timeout_s, ctx.deadline)`, DNS and connect included, with a watchdog, so a stalled or dripping receiver cannot hold a worker past its lease.
- **`script`**: section 6.6. Idempotency per batch, `max_inflight = 1`, `max_body_bytes` unbounded by default. Stdout over 1 MiB fails the attempt (`batch`) instead of truncating, since a truncated tail would silently lose emits. `HOOKSPOOL_STATE_DIR` is set (and the directory created) when `state_dir` is configured.
- **`http`**: any URL, Standard Webhooks headers, `secret` ref, optional extra static headers, `allow_insecure`. Idempotency per batch, `max_inflight = 4`, `max_body_bytes` set in config (default 1 MiB).
- **`unwrap`** (`hermes` and `http`): `unwrap = true` sends one request per event with the event's stored payload as the body, plus the optional `event_header` and `dedup_header`; idempotency is then per event (`idempotency = "per-event"`, section 6.4.1).

Action strings keep the C0 shorthand: `hermes:<route>` and `script:<name>` resolve to implicit destinations named the same; `[destinations.<name>]` tables define `http` destinations and override defaults for implicit ones. `quarantine` is a reserved destination name that never delivers.

---

### 14.3 Bobbins (service plugins)

A **bobbin** is the standard, installable plugin for one outside service: everything hookspool
needs to take that service's webhooks and turn them into work, in one directory. Sources (14.1) and
script destinations (14.2, 6.6) stay the building blocks; a bobbin bundles them with a manifest so that
a deployment enables a service with one config table instead of hand-wiring a source, a script, its
environment, its state directory and its routes. (Prior art: Hookdeck's source types, Svix's connectors,
Prefect's blocks: a typed, versioned unit with declared secrets and settings.)

#### 14.3.1 Layout

```
hookspool/bobbins/<bobbin>/          built in; or <dir>/<bobbin>/ under a configured spool.bobbin_path
    bobbin.toml                        the manifest (required)
    source.py                           a code source plugin (14.1), optional
    processor.py                        post-commit work, optional (14.3.4)
    captures/*.http                     conformance captures (ADDING_A_SOURCE.md "Capture files"), required
    README.md                           what it does, how it works, settings, outputs, limits; required
    SETUP.md                            human setup: provider-side steps, secrets to create, config table, verify, rollback; required
    AGENTS.md                           agent setup: the same procedure as numbered, executable steps (14.3.8); required
```

**One bobbin per service.** A bobbin covers exactly one outside service and contains everything needed to
run that service on top of hookspool: its functional code (source, processor), its settings and secrets,
its outputs, its captures, and its documentation and setup instructions. Bobbins are never combined (no
"google" bobbin for Gmail and Calendar: `gmail`, `gcal` and `drive` are three bobbins), and a bobbin never
imports or reads another bobbin. Code that two bobbins would both need is not shared between them; it
moves into hookspool core (14.3.7) and both call it. Several instances of one bobbin (e.g. `github-ci` and
`github-prs`) are configuration, not separate bobbins.

Bobbin names match `^[a-z][a-z0-9-]{0,39}$`; `-` maps to `_` for the module name, so two bobbins whose
names differ only by `-`/`_` are refused. Built-in bobbins import as
`hookspool.bobbins.<bobbin_with_underscores>`. A bobbin under `spool.bobbin_path` is loaded by file path
under a private module name (never added to `sys.path`), and may not share a name with a built-in.

#### 14.3.2 Manifest (`bobbin.toml`)

```toml
name = "gmail"                                   # must equal the directory name
version = "1"                                    # free text, shown by `service list`
summary = "Gmail push (Cloud Pub/Sub): new mail in watched mailboxes"

[source]                                         # exactly one of `module` or `template`
module = "source"                                # source.py in this bobbin, or a dotted module
# template = { scheme = "hmac-sha256-hex", header = "X-Hub-Signature-256", prefix = "sha256=", key = "repository.full_name", ... }
# template = "settings"                          # no fixed table: every template key comes from the instance's settings of
#                                                # the same names (14.3.2a; the `webhook` bobbin)
span = "mail"                                    # default span preset name
sender_retries = true
expect_every_s = 345600                          # recommended; applied only when the instance sets expect = true
proxy = "keep the query string (?token=...)"     # one line for the reverse-proxy recipe, optional

[secrets.push_token]                             # one table per secret
description = "the token in the Pub/Sub push endpoint URL"
# processor = true                               # the processor needs it too (14.3.4); the source gets it regardless
# required = false                              # default true; an instance may leave an optional secret out

[settings.owner]                                 # non-secret, typed settings for the processor and hooks
description = "the owner's mailbox"
type = "string"                                  # string | int | bool | list | path; required
required = true                                  # default false; a non-required setting needs `default`
default = ""                                     # must match `type`

[processor]                                      # optional; absent = events go straight to outputs
module = "processor"                             # processor.py in this bobbin
timeout_s = 300
state = true                                     # keeps state: gets a state_dir; max_inflight is 1

[outputs.scheduling]                             # one table per event_type the bobbin can produce
description = "a message asks the owner for their availability; carries free windows"
[outputs.agent-mail]
description = "new mail in the assistant's own mailbox"
```

Without a processor the outputs are the source's own event types; `[outputs."*"]` means "every accepted
event" (the only key that may stand for more than one event type). `check` validates every manifest it
loads: unknown keys, name/directory mismatch, both or neither of `module`/`template`, a span that is not a
preset, a setting without `type`, a `default` of the wrong type, a non-required setting without `default`,
and an output key that is not an event-type token or `"*"` are errors.

#### 14.3.2a Template settings, and the `webhook` bobbin (general web services)

**The rule (one rule, two forms).** A template bobbin has no `source.py`; its source is the template source
(14.1) and its keys are `scheme`, `header`, `prefix`, `param`, `callback_url`, `head_handshake`, `key`, `dedup`,
`ignore`, `event_type`, `attrs` and `sender_retries`. Its manifest says either

- `[source] template = { ... }`, a fixed table (GitHub's signature rules, Trello's handshake), or
- `[source] template = "settings"`, no fixed table: every key comes from settings.

In both forms a `[settings.<name>]` whose name is a template key **is that key's value** in the expanded
`[sources.<i>]`: the instance's value, else the setting's default unless that is empty (`""`, `[]`, `{}`),
which means unset. A fixed table never has its own keys overridden (declaring a setting for a key the table
already sets is a manifest error), so a fixed template bobbin uses settings for the one or two values only
the deployment knows (Trello's `callback_url`, the exact URL its signature covers) and `"settings"` uses them for
all. Allowed setting types follow the key: `string` for `scheme`, `header`, `prefix`, `param`, `callback_url`,
`dedup` and `event_type`; `bool` for `head_handshake` and `sender_retries`; `string` or `list` for `key` and
`ignore`; `table` for `attrs`. A template bobbin without a processor may declare no other setting (it would be
dropped silently); with a processor, the other settings are the processor's (14.3.4). The expanded source then
goes through the template source's own checks, exactly as a hand-written `[sources.<i>]` template does, so a
value it refuses fails `hookspool check` with the template's message (for example
`header: required by scheme hmac-sha256-hex`).

**The `webhook` bobbin.** `hookspool/bobbins/webhook/` is the bobbin for any web service that has no bobbin of
its own and sends a signed or tokened JSON POST. Its manifest says `template = "settings"` and declares one typed
setting per template key (`scheme` and `key` required; the rest default to unset). It has one secret
(`secret`), no processor, and the output `"*"`. Because the sender's event types cannot be listed in a
manifest, an instance of a `"settings"` bobbin may map any event-type token next to `"*"`; each becomes a route
on `event_type` (14.3.3 step 3). Its `SETUP.md` and `AGENTS.md` are a procedure for reading a provider's webhook
documentation into those settings (where the signature is, what is signed, which field identifies the object,
which header carries a delivery id, whether the sender retries) and for testing with a captured delivery. Its
`captures/` hold the kit's captures for one example instance (`captures/kit.toml`) and `captures/schemes/`
one accepted delivery per scheme.

**The scaffold.** `hookspool bobbin new <name>` copies the `webhook` bobbin's shape and freezes its settings into
a fixed `[source] template` table: the example values for the scheme, with `--set NAME=VALUE` over them (a value
is TOML, or a bare string; an empty value removes the key), each checked by the template source before
anything is written. The captures are signed for the chosen scheme. This is how a dedicated bobbin starts.
When the same service is set up through `webhook` by several deployments, that is the signal to give it its
own bobbin.

#### 14.3.3 Instances in config

```toml
[spool]
bobbin_path = ["/etc/hookspool/bobbins"]        # extra bobbin directories, searched after the built-ins

[bobbins.gmail]                                 # instance name = table key; the URL is /hooks/<instance>
bobbin = "gmail"                                 # default: the instance name
secrets.push_token = { file = "/etc/hookspool/secrets/gmail_push_token" }
secrets.cred_owner = { file = "/etc/hookspool/google/owner.json" }
settings = { owner = "owner@example.com", tz = "America/New_York" }
outputs = { scheduling = "hermes:mail-scheduling", agent-mail = "hermes:assistant-mail" }
expect = true                                    # apply the manifest's expect_every_s
# optional overrides: span, quiet_s, max_s, max_events, max_bytes, expect_every_s, dedup_window_s,
#                     max_body_bytes, timeout_s (processor), state_dir (processor; keeps existing state
#                     across a migration; it must be in the dispatcher unit's ReadWritePaths unless it is below
#                     the home, and it must be private: the processor keeps OAuth access tokens and rotated
#                     refresh tokens in it in clear text, so mode 0700, owned by the service user)
```

Two instances of one bobbin are two tables with different keys and the same `bobbin` (e.g.
`github-ci` and `github-prs`, each with its own secret).

**Expansion.** Expansion happens inside `load_config`, on every path that builds a config from a file
(startup, `check`, the intake's and the dispatcher's reload), so every other part of hookspool sees only
ordinary tables:

1. `sources.<i>`: the bobbin source (the bobbin's `source.py` or the template keys), `secrets` from the
   instance, window/expect/dedup/body overrides from the instance, and `action` = `script:<i>` when the
   bobbin has a processor, else the mapped destination of the single output (or of `"*"`).
2. With a processor, `destinations."script:<i>"` with `processor = "<i>"` (a new script key that replaces
   `argv`: the **dispatcher** builds `[sys.executable, "-I", "-m", "hookspool.processor", "<i>"]` at spawn,
   so the interpreter is always the dispatcher's own and `-I` keeps the working directory off
   `sys.path`), `timeout_s`, `state_dir` (instance override, else `<spool.home>/services/<i>`, when
   `state = true`), `max_inflight = 1`, and an `env` table (a new script key: string values only, names
   `^[A-Z][A-Z0-9_]*$`, never `HOOKSPOOL_*` from config, merged over `env_file`) holding:
   - `HOOKSPOOL_BOBBIN`, `HOOKSPOOL_BOBBIN_INSTANCE`, the bobbin's resolved directory as
     `HOOKSPOOL_BOBBIN_DIR`;
   - `HOOKSPOOL_SETTINGS`: one JSON object, setting name → typed value, defaults applied;
   - `HOOKSPOOL_DELTA_ROUTES`: the JSON of `outputs`;
   - `HOOKSPOOL_SECRET_REFS`: JSON, secret name → its **reference** (never a value) for each secret the
     manifest marks `processor = true`. `check` refuses `{ env = ... }` references for those (the child
     does not inherit the dispatcher's environment); `file` and `env_file` references resolve in the child.
3. Routes. With a processor, no routes are generated, and `check` refuses a non-tee `[[routes]]` entry with
   `source = "<i>"` whose destination is not `script:<i>` (it would bypass the processor); tee routes are
   allowed. Without a processor and with more than one output, the outputs are expanded to `[[routes]]`
   named `<i>-<event_type>` with `match = { event_type = ... }` (the source exposes `event_type` in
   `attrs`), inserted before the file's own routes; the source's `action` is the `"*"` output, and `check`
   refuses a multi-output instance without `"*"` mapped, so no event can reach a route with no action.
4. An explicit `[sources.<i>]` or `[destinations."script:<i>"]` that collides with an expansion is an
   error (no silent override). Unknown instance keys, an unknown bobbin, a missing required setting or
   secret, a setting value that does not match its `type`, a setting or output not in the manifest, and a
   processor bobbin's output mapped to a destination that does not exist are errors. An output the
   instance leaves out is dropped by the processor with one log line per batch (the deployment does not
   want it).

**Reload.** Manifests and bobbin directories are re-read on every config load. Editing a `bobbin.toml`
or a `spool.bobbin_path` directory does not touch `config.toml`'s mtime, so it takes effect at the next
`hookspool reload` (or service restart). Processor code is loaded fresh by each processor subprocess, so a
change to `processor.py` or a hooks module applies to the next batch; source code is loaded by the intake
at start and on reload.

`hookspool bobbin expand [<i>]` prints the expanded TOML with secret references as references and
`HOOKSPOOL_SECRET_REFS` shown by name only; no value is ever printed.

#### 14.3.4 Processors

`hookspool.processor` is the runtime. `deploy/scripts/hookspool_batch.py` becomes a re-export of it and
the stand-alone "copy three files" mode is retired; the delta scripts move into their bobbins. 
`python -m hookspool.processor <instance>` loads the processor module from `HOOKSPOOL_BOBBIN_DIR` and calls

```python
def process(batch: Batch, ctx: Context) -> list[dict]:
    """Return items; each is {"event_type": ..., <payload fields>}; [] means nothing to do."""
```

`Batch`: `id`, `delivery_id`, `source`, `key`, `events` (stored payloads, oldest first), `doc`.
`Context`: `settings` (typed), `secret(name)` (resolves a `processor = true` secret through the config
resolver; never logged), `http` and `oauth(secret_name)` (14.3.7), `load_state(name, default)` /
`save_state(name, obj)` (staged; applied only after the journal records the result), `hooks` (14.3.6 or
None), `log(line)` (stderr; ids and counts, never bodies), `now()`, and `fail_permanent(reason)` which
**raises** `Permanent`.

The runtime holds the state lock for the whole batch, turns items into `{"enqueue": ...}` emits addressed
through `HOOKSPOOL_DELTA_ROUTES` (`source` = instance, `key` = the item's `key` field or the batch key,
dedup id derived from the batch id and item index), journals emits and staged state under
`HOOKSPOOL_BATCH_ID`, applies the state, prints the emits and exits 0. A retry of the same batch replays
the journal without calling `process`. `Retryable` or any other exception → exit 75 (retry), with the
exception class only in the log; `Permanent` → exit 65 (the dispatcher's permanent class); its reason is
logged (300 characters) and kept with the attempt by the script destination, so it must be a short fixed phrase,
never a body or a secret. A bobbin without state has no journal: a retry calls `process` again and the
stable dedup id is the only duplicate guard. Nothing but
emit lines ever reaches stdout. Unmapped event types are dropped with one log line.

#### 14.3.5 Tooling

- `hookspool bobbin list` — bobbins found (built-in, then each `spool.bobbin_path` dir): name, version,
  origin, summary, and which instances use them.
- `hookspool bobbin show <bobbin>` — secrets, settings (type/required/default), outputs, span,
  sender_retries, proxy note.
- `hookspool bobbin expand [<instance>]` — 14.3.3.
- `hookspool bobbin new <name> [--dir D] [--set NAME=VALUE ...] [--code] [--processor]` — a copy of the `webhook`
  bobbin with its settings frozen into a fixed `[source] template` (14.3.2a): manifest, an accepted and a
  rejected capture signed for the scheme, README, SETUP.md, AGENTS.md (`--code`: a `source.py` instead of the
  template; `--processor`: also a `processor.py`). It passes the conformance kit once its TODO placeholders are
  filled in (until then the kit's `todos` check is its one failure).
- **Conformance** (`hookspool.testing.bobbinkit`, run by CI for every bobbin in the repo and by
  `.github/scripts/plugin_ci.py` for every changed bobbin): manifest valid; the 14.3.7 rules; the source
  passes the source kit with each capture; the processor (if any) runs each capture's payload through
  `process` with a fake `Context` (stub HTTP), its items' event types are declared outputs, a second run
  of the same batch id replays the journal, and nothing but emits reaches stdout.
  `hookspool bobbin check <dir> [--report text|md|json] [--strict] [--base REF]` runs the kit on one bobbin and prints a
  scorecard (each check, pass / fail / skip, every problem as `file:line` with how to fix it); CI runs that same command,
  so no check is CI-only. The checks: `manifest` (including one service per bobbin), `files`, `todos`, `core-only`
  (14.3.7), `docs` (14.3.8), `captures` (an accepted and a rejected capture, each `# expect:` holds, no secret-looking
  value or real-looking personal data), `source-kit`, `processor`, `tests` (the bobbin's own, network blocked), and the
  advisory `scope` (`bobbin-only` or `touches-core`). Test values and stub API answers live in `captures/kit.toml`.

  **Merge criteria.** A pull request that changes only `hookspool/bobbins/<name>/` (and its own tests), whose scorecard
  passes every required check, and whose author has ticked the template's three boxes (the provider's signature
  documentation is linked and matches the declared scheme; the captures came from a real delivery and were redacted; the
  author can maintain the bobbin or accepts its removal if it breaks) is the expected merge. What the maintainer still
  checks by hand is the provider's documentation against the declared scheme and the capture bodies; a `touches-core`
  diff is reviewed as a core change. [BOBBIN_REVIEW.md](BOBBIN_REVIEW.md) is the reviewer's page.

#### 14.3.6 Private customisation

A deployment never edits a built-in bobbin. Behaviour changes through:

1. **Settings** (14.3.2).
2. **A hooks module**: a bobbin that supports hooks declares `[settings.hooks]` with `type = "path"` and
   documents the functions it calls (e.g. gmail calls `hooks.classify(message, account, ctx) -> dict | None`
   before its own rules; gcal calls `hooks.travel(event, day_events, ctx) -> dict | None` for travel blocks). The
   runtime loads it by file path and passes it as `ctx.hooks`. Hooks are deployment code: the 14.3.7
   rules apply to bobbins, not to hooks.
3. **A private bobbin** in a `spool.bobbin_path` directory, when the service is not in the repo at all.

Code loaded by path (a `spool.bobbin_path` directory, a hooks module) must be an absolute path to a regular
file or directory that is not group- or other-writable and is owned by root or the user hookspool runs
as; `check` verifies this as it does for secret files.

#### 14.3.7 Bobbins call core; they never re-implement it, and never each other

A bobbin contains only what is specific to its service: which header is the signature, which fields
are the key and the dedup id, which API calls answer "what changed", and how a change becomes an output.
Everything that is the same for two services is core, and a bobbin calls it:

| need | core API (the only allowed way) |
|---|---|
| signature / token / timestamp checks | `hookspool.verify` (via `hookspool.plugin.verify`), or the `template` source |
| a sender's published verification keys (JWKS) | `hookspool.verify` key fetch and cache (moved out of the plaid source) |
| JSON paths, headers, dedup ids, body hashes | `hookspool.plugin` (`get_path`, `Request`, `dedup`) |
| durable state, journal, replay, atomic writes, locking | `ctx.load_state` / `ctx.save_state` (the runtime holds the lock for the batch) |
| results to the agent or another destination | return items from `process`; the runtime emits them |
| retries, backoff, timeouts, dead-letter | the dispatcher: raise `Retryable` / `Permanent` (or `ctx.fail_permanent`); never loop or sleep |
| HTTP to the provider's API | `ctx.http` (`hookspool/net.py`): timeouts, JSON, status → `Retryable`/`Permanent`, no bodies or query strings in errors or logs |
| OAuth 2 refresh-token flows | `ctx.oauth(<secret name>)` (`hookspool/net.py`): reads the credential through `ctx.secret`, caches access tokens in runtime state, refreshes on expiry or one 401, and **never rewrites the credential file** (a rotated refresh token is kept in runtime state and used from then on) |
| secrets | `ctx.secret(name)` (processor, through `net.make_context_extras`: a name the processor was not given is `Permanent`, an unreadable reference `Retryable`) / the source's `Secrets` (source) |
| settings | `ctx.settings`; a bobbin never reads the environment |
| logging | `ctx.log` (processor) / the intake's logger (source); a bobbin never opens log files |
| time | `ctx.now()` / `req.now` |
| helpers shared by several bobbins of one provider (Google free/busy, Gmail history paging) | `hookspool/providers/<provider>.py`, written once on `ctx.http` / `ctx.oauth` |

Enforcement: the conformance kit walks the AST of every bobbin module and fails on imports of `hmac`,
`urllib`, `http`, `requests`, `httpx`, `aiohttp`, `socket`, `ssl`, `subprocess`, `asyncio`, `threading`,
`multiprocessing`, `signal`, `sqlite3`, `fcntl`, `tempfile`, `shutil`, `pickle`, `logging`, `importlib`;
on calls to `open`, `print`, `exec`, `eval`, `compile`, `__import__`, `os.*` other than `os.path.*`,
`time.sleep`, any `.write_text`/`.write_bytes`/`.open`/`.unlink` method, and on any use of `sys.path` or
`os.environ`, and on any import of `hookspool.bobbins` (a bobbin never imports another bobbin, or itself by
package path; its own files are siblings loaded by the runtime). A bobbin that needs something core lacks gets it added to core (a PR to `hookspool/net.py`,
`hookspool/verify.py`, `hookspool/providers/`, the runtime or the plugin API), never a private copy.
`.github/scripts/plugin_ci.py` runs the same check on every changed bobbin.

#### 14.3.8 Setup instructions (SETUP.md, AGENTS.md)

Every bobbin ships two setup documents describing the same procedure:

- `SETUP.md` is for a person: what to create at the provider (app, webhook, push subscription, watch
  channel), which secrets to save where, the `[bobbins.<instance>]` table to add, the reverse-proxy line,
  how to verify (a test delivery, `hookspool list --source <instance>`), the sweep to install if the
  sender does not retry, and how to roll back.
- `AGENTS.md` is for an agent doing the same setup, following the agents.md convention: numbered steps,
  each with its goal, the exact command or API call (with placeholders in `<ANGLE_CASE>`), what to check
  afterwards and what to do if the check fails. It opens with a **Needs from the human** list (accounts,
  consents, values only a person can provide, any step that spends money or changes a shared account),
  and it never asks the agent to handle a secret's value beyond writing it to the referenced file.
  Steps that change something at the provider are marked `outward` so an agent asks before running them.

The conformance kit checks that both files exist, that every declared secret and required setting
appears in both, that `AGENTS.md` has the **Needs from the human** list and numbered steps, and that the
config table shown in both expands without errors.

#### 14.3.9 Compatibility

`[sources.*]` tables keep working exactly as now (they are what an expansion produces). The built-in code
sources move into their bobbins; `hookspool.sources.<name>` stays importable as a re-export so existing
configs with `[sources.gmail]` (implicit module) still load. `examples/sources/*.toml` become bobbins on
the `template` source.

## 15. Configuration

`config.toml` in the state directory (`tomllib`, stdlib). TOML only. Unknown keys are errors in `hookspool check`, warnings at runtime. The dispatcher and intake reload on mtime change (checked every tick and every 5 s respectively); an invalid file keeps the previous config and opens a `config:file` issue. `hookspool reload` validates the file and touches it so both processes pick it up together.

```toml
[spool]
home = "/var/lib/hookspool"          # HOOKSPOOL_HOME overrides
db = "spool.db"                      # relative to home
store_raw = true                     # keep original bodies for replay/verification tests

[intake]
listen = "127.0.0.1:8650"            # or "unix:/run/hookspool/intake.sock"
max_body_bytes = 1048576
busy_timeout_s = 10
threads = 16

[dispatch]
parallel = 4
poll_s = 1.0
heartbeat_s = 10
lease_grace_s = 60
max_attempts = 64                    # counted (batch-class) attempts only
backoff_base_s = 15
backoff_cap_s = 300
breaker = { trip_after = 3, cooldown_initial_s = 30, cooldown_max_s = 600 }

[coalesce]                           # defaults for every window field; route and source override per field
quiet_s = 0
max_s = 0
max_events = 500
max_bytes = 786432                   # 768 KiB; `check` requires <= 0.75 × destination.max_body_bytes

[limits]
min_free_bytes = 536870912

[retention]
delivered_days = 7
attempts_days = 30
dropped_days = 7
issues_days = 90

[alerts]
argv = ["logger", "-t", "hookspool"]  # the deployment replaces this with its chat sender
dry = false
rate_limit_s = 1800
timeout_s = 60

[hermes]
base_url = "http://127.0.0.1:8644"
subscriptions_path = "~/.hermes/webhook_subscriptions.json"
timeout_s = 30
max_body_bytes = 1048576              # must match the gateway's webhook max_body_bytes
legacy_signature = true               # also send X-Hub-Signature-256

[spans]                                # presets (quiet_s, max_s); add your own
burst = { quiet_s = 120, max_s = 600 }
mail = { quiet_s = 30, max_s = 120 }
calendar = { quiet_s = 45, max_s = 180 }
sync = { quiet_s = 60, max_s = 300 }
immediate = { quiet_s = 0, max_s = 0 }

[sources.github]
span = "burst"
secrets.secret = { file = "/etc/hookspool/secrets/github" }   # or { env = "…" } / { env_file = "…", key = "…" }
dedup_window_s = 86400
expect_every_s = 0                     # 0 = no liveness expectation
max_body_bytes = 786432                # per-source override; `check` requires <= the route's max_bytes

[sources.plaid]
span = "sync"
secrets.client_id = { env_file = "/etc/hookspool/plaid.env", key = "PLAID_CLIENT_ID" }
secrets.secret    = { env_file = "/etc/hookspool/plaid.env", key = "PLAID_SECRET" }
env = "production"
dedup_window_s = 600

[sources.zoom]
span = "immediate"
secrets.webhook_secret = { file = "…" }

[sources.gcal]
module = "hookspool.sources.gcal"
span = "calendar"
secrets.channel_token = { file = "…" }
action = "script:calendar-refresh"
expect_every_s = 172800

[[routes]]
name = "github-ci"
source = "github"
match = { class = "ci" }
destination = "hermes:github-ci"

[[routes]]
name = "github-prs"
source = "github"
match = { class = "pr" }
destination = "hermes:github-prs"
quiet_s = 60                           # route-level override of one field; the others inherit

[destinations."hermes:github-ci"]
max_inflight = 2

[destinations."script:calendar-refresh"]
argv = ["/usr/bin/python3", "/opt/scripts/refresh_events.py"]
cwd = "/opt/scripts"
env_file = "/etc/hookspool/refresh.env"
timeout_s = 300
state_dir = "/var/lib/hookspool/scripts/gcal"   # listed in the unit's ReadWritePaths by deploy/README.md

[destinations.audit]
type = "http"
url = "https://example.invalid/hooks/audit"
secret = { file = "/etc/hookspool/secrets/audit" }
headers = { "X-Env" = "prod" }
max_body_bytes = 1048576
```

Precedence for a window field: route → source → `span` preset → `[coalesce]`. Precedence for paths: CLI flag → env (`HOOKSPOOL_HOME`, `HOOKSPOOL_CONFIG`, `HOOKSPOOL_DB`) → config → default.

`hookspool check` also: imports every plugin; resolves every secret ref (readable, mode; `env` refs noted as non-rotating); refuses `auth_headers` in `keep_headers`; validates routes and destinations (and the tee rules of section 14.1); checks `quiet_s <= max_s`, `max_bytes <= 0.75 × destination.max_body_bytes`, `source.max_body_bytes <= route.max_bytes`, `dedup_window_s <= retention.delivered_days`; reads `hermes.subscriptions_path` and fails on `events` or `coalesce` set on any route a hookspool destination feeds; `quick_check`s the DB.

---

## 16. CLI

`hookspool [--home DIR] [--config FILE] [--json] <command>`

| command | does |
|---|---|
| `intake` / `dispatch` / `run` | the services (`run` = both in one process) |
| `status` | section 11.1 |
| `list [--state S] [--source S] [--destination D] [--limit N]` | batches, newest first, no payloads (`--destination quarantine` lists quarantined requests) |
| `show <batch> [--events] [--payload] [--attempts]` | one batch; `--events` lists ids, types, sizes, dedup ids and exclusion without payloads; payloads only on request, printed to a tty with a warning if secrets could be in them |
| `replay <batch>… \| --dead [--destination D] [--source S] [--since T]` | dead → retry, due now, attempts reset (bulk retry); on a quarantine batch: reclassify through the plugin |
| `retry <batch>` | retry → due now (skip the backoff) |
| `split <batch> [--max-events N \| --max-bytes B \| --at <event_id>]` | cut a `sealed`/`retry`/`dead` batch into new `open` batches (same group, `not_before = now`, audit row); the original becomes `dropped` with `result = "split"` |
| `exclude <batch> <event_id>… --yes` | set `events.excluded_at`; recompute `content_hash`; the event stays stored and listed |
| `drop <batch>… --yes` | → dropped (audit row, never automatic) |
| `pause <destination> [--reason …]` / `resume <destination>` | operator gate (destinations only) |
| `issues [list\|ack\|resolve\|ignore <id>]` | section 11.4 |
| `enqueue --source S --key K [--destination D] [--event-type T] [--dedup-id ID] (--file F \| stdin)` | manual or sweep injection, same validation as the intake |
| `source test <name> --request FILE` | replays a captured HTTP request (`.http` text: start line, headers, blank line, body) through verify/challenge/ignore/key/dedup/attrs offline; the way to develop a plugin against real captures with test secrets |
| `check` | section 15; exit 2 on errors. Used as `ExecStartPre` |
| `reload` | `check`, then touch the config so both processes reload together |
| `migrate [--dry-run]` | apply pending schema migrations (takes `dispatch.lock`; refuses while the dispatcher runs) |
| `prune [--vacuum]` | run retention now |
| `export [--state S] [--since T] > file.jsonl` | batches + events + attempts as JSONL (backup, forensics) |
| `metrics` | print the `/metrics` text |

Exit codes: 0 ok, 1 "not found / nothing to do", 2 config or schema error, 3 lock held / already running.

---

## 17. Schema versions and upgrades

- `meta.schema_version` (also mirrored in `PRAGMA user_version`). Migrations are numbered SQL files under `hookspool/migrations/NNNN_<name>.sql`, applied in order inside one `BEGIN IMMEDIATE` transaction each (a migration that must rebuild a table under `foreign_keys=ON` turns the pragma off before `BEGIN` and runs `PRAGMA foreign_key_check` after). Only two callers apply them: the dispatcher at startup and `hookspool migrate`, both under `dispatch.lock`. The intake and the other CLI commands only read the version: older than known → exit 2 with "run hookspool migrate"; newer than known → exit 2 (downgrade is not supported; restore the older binary or the backup).
- v2 ships a single migration, `0001_v2.sql`, which creates the whole schema. The v1 draft on `l/T1` was never deployed and left no database to migrate; nothing supports its schema or its JSON config.
- Upgrades on a live host: stop the dispatcher, `hookspool migrate`, start. The intake may keep running during an additive migration; a non-additive one needs both stopped (a few seconds of 502; section 9 says which senders retry, so schedule it in a quiet hour).

---

## 18. Test strategy

All tests are hermetic: temp state dir, fake clock injected as `now()`, no network, stub HTTP server for Hermes/`http`, `HOOKSPOOL_ALERT_DRY=1`. The `l/T1` `tests/conftest.py` (fake clock, sentinel, stub server, `write_subs`) is reused.

| layer | what | how |
|---|---|---|
| store | enqueue/join/seal (size, readiness)/claim/fenced mark transactions; readiness truth table; `content_hash` stable across attempts and recomputed by `split`/`exclude`; dedup window and the dead/dropped hit rules; quarantine and reclassify; retention | unit tests with the fake clock; a randomised model test (stdlib `random`, seeded) that replays 10 000 enqueue/seal/claim/complete operations against a Python model of the state machine and compares |
| crash safety | `kill -9` at every `_test_hook` point in enqueue, seal, claim, deliver, mark-with-emits, retention; power-loss simulation by copying `spool.db` + WAL mid-transaction and reopening | `multiprocessing` children |
| concurrency | 4 writer processes × 1 000 enqueues; one dispatcher; every event delivered exactly once per batch; per-key order preserved; parallel across keys; no batch over `max_events`/`max_bytes` with the gate closed for the whole run; two dispatchers → second exits 3 | subprocess tests |
| dispatcher | backoff schedule; `Retry-After`; counted vs uncounted per class; breaker trip on one key; rotating probe and poison-by-comparison; single-batch destination stays open; re-trip only for probes; `probe_until`; pause/resume; `config` retry re-dued by a reload; clock back (time stands still) and forward; lease recovery at startup, never for a live worker; worker deadline; fenced mark discards a late result; issues open/resolve/alert-once; alert `alerted_at` only on exit 0 | fake clock, stub destination |
| destinations | hermes body/headers/signatures (verified with an independent Standard Webhooks check in the test); every Hermes response code and `status` → outcome/notice; fresh secret per attempt; per-attempt vs per-batch ids by type; script stdin/env/timeout/process-group kill/emit atomicity; http TLS/redirect/insecure rules | stub servers, `subprocess` scripts in the temp dir |
| intake + sources | `tests/sourcekit.py` runs the same matrix on every plugin (section 14.1) plus per-source captures (`tests/captures/<source>/*.http`, signed with test secrets); Zoom validation with a bad signature → 401 and no token in the body; Plaid unknown-kid fetch guard | `ThreadingHTTPServer` on an ephemeral port |
| verify helpers | known-answer vectors for every scheme (GitHub, Trello SHA-1+callback, Zoom v0, Standard Webhooks with `whsec_` and raw keys, Plaid ES256 with a generated key and JWK, static token, `t=,v1=`), stale and future timestamps; constant-time wrapper used exclusively (grep test) | unit |
| security | no sentinel in any log line (autouse fixture); no secret value in the DB (`SELECT` scan, quarantine rows included); `auth_headers` never stored; 401 bodies empty; file modes | unit |
| CLI | every command against a seeded DB; `check` finds each class of misconfiguration (including Hermes `events`/`coalesce` and the `max_bytes` bounds); `split`/`exclude`/`replay` round trips; `source test` on captures | `main(argv)` in-process |
| soak (optional, `tests/soak.py`) | 1 h with random bursts, a flapping stub and periodic `kill -9` of the dispatcher; asserts: no batch lost, no batch delivered with two different `content_hash`es, no batch over its bounds, CPU under a budget | manual / CI nightly |

Acceptance for the whole suite: `python -m pytest -q` green on 3.11 and the newest stable Python, under 90 s excluding the soak.

---

## 19. What changed from the v1 draft and why

| v1 (l/T1 draft) | v2 | why |
|---|---|---|
| `hookbus`, JSON config, state under the Hermes profile | `hookspool`, TOML (`tomllib`), `HOOKSPOOL_HOME` default `/var/lib/hookspool` | public, generic; comments in config; stdlib |
| Failed batch returns to `open` and keeps accepting events | sealed on size at enqueue or on readiness in the tick; `sealed` and `retry` states | stable content across attempts; bounded documents whatever the destination does |
| `dead` after 72 h of failures | `dead` only on permanent, poison or `max_attempts` counted (batch-class) attempts; transport failures never count | a multi-day outage must never kill batches, on any destination |
| No destination state | breaker + operator pause per destination, rotating probe, issues | stop hammering a dead gateway; one alert instead of one per batch; a poison batch cannot wedge its destination |
| `UNIQUE(source, dedup_id)` forever | `dedup` table with a per-source window | bounded; provider redeliveries after the window are honoured |
| `X-Request-ID` only | Standard Webhooks headers + legacy `X-Hub-Signature-256`; per-attempt ids for Hermes, per-batch for the rest, fixed by type | interoperable; correct against Hermes' admit-before-process idempotency |
| Alert command hard-coded to a Hermes CLI call | `alerts.argv`, any command, run off the main loop | generic |
| Source list hard-coded in `DEFAULTS` | sources are config + plugins; spans are presets | generic |
| `prune` of delivered only | retention per table, dead never pruned | bounded growth without losing anything an operator might need |
| Single lease field reused as `next_attempt_at` | explicit `lease_until`/`lease_owner` fence, worker deadline, heartbeat, `flock` | at most one recorded result per attempt |
| 500 on a plugin error | quarantine + 202 | GitHub does not retry; our bugs must not lose events |
| Kept unchanged | commit-before-ack, `BEGIN IMMEDIATE` per write, WAL + FULL, FIFO per key, emits in the same transaction as the mark, fresh secret per attempt, `_test_hook` crash points, no bodies in logs, C0 plugin surface | they were right |

---

## 20. Addressed review

`.ladder/ARCH_REVIEW.md` reviewed the first v2 draft. Every blocker and major is resolved as follows; minors are accepted unless listed under "rejected or narrowed".

| item | resolution |
|---|---|
| B1 signing oracle | §9/§10/§14.1: order is body cap → `verify` → `challenge`; `challenge_unauthenticated` flag for constant, secret-free handshakes only; GitHub `ping` is an `ignore()` case; sourcekit tests a handshake with a bad signature. |
| B2 single-key destination dies in 5 h | §4.2/§6.5: breaker trips on consecutive transport failures on the destination; transport failures are uncounted; only `batch`-class outcomes count toward `max_attempts`. |
| B3 batches only seal at claim | §4.1/§5: explicit `sealed` state; size seals in the enqueue transaction (with a pre-join check so `max_bytes` is a true bound); readiness seals in the tick before any gate; join on the single `open` batch; `max_bytes` default 768 KiB and `check` bounds it by the destination's `max_body_bytes` (Hermes' default is 1 MiB). |
| B4 "the sender retries" is false for GitHub | §9: quarantine instead of 5xx after `verify`; honest sender-retry table; §13 GitHub redelivery sweep; `sender_retries` declared per source; `plugin_error_streak` gone. |
| M1 lease recovery double-delivers | §6.2: `lease_owner` fences every mark; worker deadline at `lease_until - 5 s`; the tick never recovers a live worker's lease; the 2×timeout rule is gone. |
| M2 clock guard rewrites facts | §2/§12: `now_eff = max(wall, last_tick_at)`; `shift_clock` gone; `first_at`/`last_at`/`received_at` immutable. |
| M3 poison wedges a destination | §4.2: rotating probe, poison by comparison, `last_probe_batch_id`; `poison_after` gone. |
| M4 config problems go `dead` | §6.5: `config` outcome class, retry at the cap, uncounted, with an issue; re-dued on a successful reload; `permanent` is 400/413/415/422 and 3xx on `http`; `hookspool reload`. |
| M5 `per-batch` on Hermes loses a batch | §6.4/§14.2: idempotency fixed by destination type; config key removed; `duplicate` documented as admission only. |
| M6 Hermes `events` filter drops work silently | §5/§15: `check` reads `subscriptions_path` and fails on `events`/`coalesce`; `ignored`/`coalesced` are `ok` with a `config` issue; the document carries `event_types`. |
| M7 migration of a database that does not exist | §17: single `0001_v2.sql`, TOML only; only the dispatcher and `migrate` apply migrations, under `dispatch.lock`; the intake exits 2 on an old schema. |
| M8 "identical bytes" is false | §3/§6.4: `content_hash` covers `events` + `event_meta` only; the promise is identical events in identical order. |
| M9 no way to repair a batch | §16: `split`, `exclude`, `show --events`; `events.excluded_at`. |
| m1–m26, m29–m33 | accepted as written: per-field window inheritance and the `quiet_s > max_s` warning; one-poll overshoot stated; join rule rewritten; gcal/gmail dedup ids and Plaid's 600 s window; re-trip only for probes; `probe_until`; `destinations.inflight` removed; exit 3; no FK on `dedup.event_id` and the retention warning; dead/dropped dedup hits; "at least `dedup_window_s`"; archive removed; two-sided tolerance; Plaid kid guard; 411 on chunked plus proxy buffering; `env` refs do not rotate; GitHub replay window stated; alerts off the main loop with `alerted_at` on exit 0; `/healthz` write probe at most every 30 s; heartbeat 10 s; route pause removed; `ReadWritePaths`/`state_dir`; migrations only by `migrate`/`dispatch`; per-destination ages in `status`; `min_free_bytes` only; both `rate_per_min` removed; `delay_s` removed; the PLAN contracts updated. |
| m18 Pub/Sub OIDC | accepted as a noted later hardening (§10), not built now. |
| m27 persisted counters | **narrowed**: persisted counters are gone as asked, but counters computed from pruned tables are not monotonic, so they are not exposed as Prometheus counters. Intake counters are per-process in memory (reset on restart, which `rate()` tolerates); dispatcher figures are gauges over the retention window (§11.2). |
| m28 limits | accepted; `min_free_bytes` is the only limit. |
| m11 dedup hit on a `dropped` batch | **narrowed**: a hit on a `dead` batch replays it; a hit on a `dropped` batch enqueues a new event instead of resurrecting what the operator discarded (§7). |
