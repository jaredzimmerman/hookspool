# Changelog

All notable changes are recorded here. The format follows Keep a Changelog, and
the project follows Semantic Versioning once it reaches 1.0; until then a minor version may change
an interface, and the entry will say so.

## [Unreleased]

The changes below will ship as 0.2.0.

### Added

- `hookspool.processor`: the runtime for service-package processors (`python -m hookspool.processor <instance>`): a
  per-batch state lock, a journal so a retried batch replays without calling `process` again, staged state writes, emits
  addressed through the deployment's routes, typed settings, and exit codes 0 (done), 75 (retry), 65 (permanent). The
  stand-alone copy mode of `deploy/scripts/hookspool_batch.py` is retired: it now re-exports the runtime, so the delta
  scripts must run under the interpreter hookspool is installed in.
- `unwrap = true` for `hermes` and `http` destinations: a batch is delivered as one request per event, in order, with the
  event's stored payload as the body, for receivers that expect the sender's original payload (a Hermes route whose prompt
  template reads `{pull_request.title}`). Each request carries Standard Webhooks headers with a per-event `webhook-id`
  (`hs_<instance>_<batch>_<event id>`, stable across attempts), `X-Hub-Signature-256` over that body, and the optional
  `event_header` (the event's type) and `dedup_header` (its dedup id). The batch is delivered only when every event was; the
  first failure stops the attempt, is classified as before, and retries the whole batch, whose already-accepted events the
  receiver answers `duplicate`. Hermes `ignored`/`coalesced` answers open no issue in this mode, and `hookspool check`
  allows the gateway route's own `events` filter and `coalesce` there, and warns when an unwrapped route coalesces.
- Attempt rows record an unwrap delivery's progress: `events_delivered` and `failed_event_id` (ids and counts, no bodies).
- Tee routes: `tee = true` on a `[[routes]]` entry gives that route a copy of each event it matches without claiming the
  event, which still goes to the first matching non-tee route or the source's `action`, so adding a tee route never changes
  where an event already went. Each copy is its own event in its own batch with the tee route's window; retry, the breaker,
  dead batches, `replay`, `exclude`, `split` and `drop` act on each copy, and a failing tee destination never delays the
  primary. All copies of a request are committed in one transaction before the 2xx; dedup and `ignore` run once per request.
  `check` refuses a tee destination that is the source's `action` or another route's destination on the same source, and
  more than 8 tee routes per source. The intake logs one `queued` line per copy (`tee: true` on the copies), and
  `hookspool source test` lists the `tees` a request would feed.

### Changed

- Spool schema 2 (migration `0002_unwrap.sql` adds the two attempt columns). Stop the dispatcher and run `hookspool migrate`
  (deploy/README.md, Upgrading): both services run `hookspool check` before they start and refuse a schema 1 spool, so a
  restart alone does not migrate it.
- The message for an old or missing spool now says to stop the dispatcher and run `hookspool migrate`, and points to
  deploy/README.md; a Hermes 413 under `unwrap` says to exclude the event or raise `max_body_bytes` (one event, not a batch).

### Fixed

- The dispatcher's hot reload validated the new file without loading source plugins, so any config
  with a `template` source was refused ("unknown key") and the dispatcher kept running on the old
  config while the intake had already reloaded. Reload now validates the way `check` and startup do.

## [0.1.0]

### Added

- The spool: one SQLite file in WAL mode with `synchronous=FULL`. A request is committed before its sender gets a 2xx;
  every write is a single `BEGIN IMMEDIATE` transaction.
- The intake (`hookspool intake`): a loopback HTTP server with one path per source, a fixed pipeline of body cap, signature
  verification, handshake, classification and enqueue, and `/healthz` and `/metrics`. A bad signature is 401 with an empty
  body, a chunked request 411, an oversized body 413, and a spool that cannot keep the write 503. A plugin that raises
  after verification quarantines the request and answers 202 instead of failing it.
- Coalescing per `(route, key)` with a quiet window, a maximum wait, an event limit and a byte limit. Batches seal on size
  inside the enqueue transaction and on readiness in the dispatcher, independent of any destination. Window values come
  from named span presets (`burst`, `mail`, `calendar`, `sync`, `immediate`) and are inherited one field at a time from
  route, source, span and the global default.
- The dispatcher (`hookspool dispatch`): a single deliverer under an exclusive lock, with fenced leases, strict order per
  key and parallelism across keys, retry with backoff and `Retry-After`, a per-destination circuit breaker with a rotating
  probe that pauses but never drops, issues, alerts that run off the main loop, and hourly retention.
- Destinations: `hermes:<route>` (reads the route's secret fresh on each attempt), `script:<name>` (batch on stdin, durable
  `{"enqueue": ...}` emits committed with the delivery) and `http` (signed POST to any HTTPS endpoint). Deliveries carry
  Standard Webhooks headers, plus the legacy `X-Hub-Signature-256` for Hermes.
- Sources: `github`, `trello` and `kaneo` as config-only templates (`examples/sources/`), and `zoom`, `plaid`, `gmail` and
  `gcal` as code plugins. The `template` source builds a source from a TOML table (five verification schemes, dotted-path
  and header selectors, no expression language).
- The extension model: [docs/ADDING_A_SOURCE.md](docs/ADDING_A_SOURCE.md), [docs/ADDING_A_DESTINATION.md](docs/ADDING_A_DESTINATION.md),
  a conformance kit (`tests/sourcekit.py`) that exercises every source the same way, and `hookspool source test` to run a
  captured request through a source offline.
- The command line: `status`, `list`, `show`, `replay`, `retry`, `split`, `exclude`, `drop`, `pause`, `resume`, `issues`,
  `enqueue`, `source test`, `check`, `reload`, `migrate`, `prune`, `export` and `metrics`.
- Retention that never prunes dead batches, backups through `VACUUM INTO` and `hookspool export`, and schema migrations
  applied only by the dispatcher and `hookspool migrate`.
- Deployment files in `deploy/`: systemd units, an nginx example, a complete example config, a rollout guide with a rollback
  for every step, a gateway-down drill, the sweep table, delta scripts for Gmail and Calendar, and a GitHub redelivery sweep.
- Documentation: the README, an architecture document, a configuration reference, a command reference, an operations guide,
  and a test that fails when the docs and the code disagree on a command, an option or a config key.
- Packaging: `pyproject.toml` (setuptools), an MIT license, and the `plaid` and `dev` extras.
