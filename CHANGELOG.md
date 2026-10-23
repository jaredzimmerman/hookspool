# Changelog

All notable changes are recorded here. The format follows Keep a Changelog, and
the project follows Semantic Versioning once it reaches 1.0; until then a minor version may change
an interface, and the entry will say so.

## [Unreleased]

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
