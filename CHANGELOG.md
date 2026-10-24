# Changelog

All notable changes are recorded here. The format follows Keep a Changelog, and
the project follows Semantic Versioning once it reaches 1.0; until then a minor version may change
an interface, and the entry will say so.

## [Unreleased]

The changes below will ship as 0.2.0.

### Added

- The `webhook` bobbin: any web service that sends a signed or tokened JSON POST, configured entirely by settings
  (`[source] template = "settings"`; five schemes). One rule now maps a bobbin setting named after a template key to that
  key's value, for a fixed template (Trello's `callback_url`) and for `"settings"` alike; settings may be a `table`, and an
  instance of a `"settings"` bobbin may map any event type next to `"*"`. `hookspool bobbin new <name>` now copies the
  `webhook` shape and freezes its settings into a fixed template (`--set NAME=VALUE`, `--code` for a `source.py`). An
  empty setting value is unset; `check` reports an error when outputs are mapped per event type without
  `attrs.event_type`; a scaffolded `hmac-sha1-base64-url` bobbin keeps `callback_url` as a required setting.
- The `drive` bobbin (`hookspool/bobbins/drive/`): Google Drive push (a watch channel on the change feed) with a processor
  that reads `changes.list` from a stored page token and reports `file-changed`, `file-created` and `file-shared` items
  (file id, name, MIME type, modified time, actor, link; never contents). It drops the configured accounts' own edits,
  files in the trash and listed MIME types, re-bootstraps quietly when Drive rejects the page token, and replays a retried
  batch from the journal. The Drive change-feed calls live in `hookspool.providers.google` next to the channel helpers the
  `gcal` bobbin also uses.
- `hookspool bobbin check <dir>`: the bobbin conformance kit (`hookspool.testing.bobbinkit`) on one directory, printed as a
  scorecard (`--report text|md|json`, `--strict`, `--base`): manifest and one service per bobbin, files, TODOs, the 14.3.7
  core-only rules, SETUP.md / AGENTS.md, captures (including a scan for secrets and personal data), the source kit per
  capture, the processor through the real runtime with stub HTTP and the network blocked, the bobbin's own tests, and the
  advisory scope. CI runs the same command on every changed bobbin (`.github/workflows/bobbin.yml`); a separate
  `bobbin-label.yml` run labels the pull request `bobbin-only` or `touches-core` and keeps one scorecard comment.
  `docs/bobbins/review.md` gives the merge criteria. `hookspool bobbin new` now also writes SETUP.md, AGENTS.md and a
  rejected capture, and passes the kit once its TODOs are filled in. The source kit moved to `hookspool.testing.sourcekit`
  (`tests/sourcekit.py` re-exports it).
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

- `hookspool/net.py`, the network module for service processors: `Http` (JSON over HTTP with a total timeout, statuses and
  failures mapped to `Retryable` or `Permanent`, a TLS certificate failure `Permanent`, no bodies or query strings in
  errors or logs, redirects never followed, proxies from the environment), `OAuth` refresh-token flows with the access
  token cached in the runtime's state (the credential file is never written; a rotated refresh token is kept in state and
  handed to an optional `persist` callback at once), `SecretAccess` and `make_context_extras(state, secret_resolver)`.
- `hookspool.verify.KeyCache` and `jwks_fetcher`: a shared cache of a sender's verification keys (TTL, negative TTL, one
  fetch per cooldown, stale keys served while the sender is unreachable).

- The `gmail` and `gcal` bobbins (`hookspool/bobbins/`): the Gmail and Calendar push sources with their delta
  logic as processors, settings instead of environment variables, one credential secret per Google account, a
  `hooks.classify` hook for private mail hand-offs, a `hooks.travel` hook for travel blocks, and the outputs
  `scheduling`, `agent-mail`, `agent-schedule`, `mailbox-changed` and `conflict`, `brief`, `changed` (the last replaces the
  script that was started on the side). `hookspool/providers/google.py` holds what the Google bobbins share: the push-channel
  check (token, `sync` handshake, header payload) and the Gmail, Calendar and free/busy calls on `ctx.oauth`.
- A bobbin secret may be marked `required = false`, so an instance can leave it out.
- The `zoom` and `plaid` bobbins (`hookspool/bobbins/`): the Zoom event subscription and the Plaid webhook sources, moved
  from `hookspool/sources/` with their captures (`hookspool.sources.zoom` and `.plaid` still import and are the bobbins' own
  modules). Both now call core only (ARCHITECTURE 14.3.7): Zoom's HMAC and URL validation answer through `hookspool.verify`,
  and Plaid's key fetch and cache are `verify.key_fetcher` and `verify.shared_key_cache`, called through `hookspool/net.py`, with no
  `urllib`, `threading` or `logging` of the bobbin's own. Plaid's `env` is the bobbin setting `env`.
- `hookspool.verify.key_fetcher` (a key fetch for a per-key endpoint or a JWKS URL, with the "sender has no such key" refusals
  mapped to None), `verify.shared_key_cache` and `reset_shared_key_caches`, and `KeyCache.lookup(..., fetch=)` for a fetch that
  needs the request's credentials; a failed key fetch is logged once by `KeyCache` (class, HTTP status and the sender's short
  error code, never a body; only a `net` error's status and code, so a caller's own exception adds nothing).
- A bad Plaid `env` is refused when the config loads (`hookspool check` reports it) and not by a 503 on every webhook; a source
  module's `bind` may return None to keep the module and only validate the config.
- A bobbin manifest's `[source] config` refuses the names of source table keys the runtime sets (`module`, `action`, `secrets`,
  `span`, the window overrides and so on).
- A bobbin manifest's `[source] config = ["env", ...]` names the settings that become keys of the source's own table (the
  source module's `config_keys`), for a code source that reads them.
- The conformance kit answers a source's own HTTP calls (a sender's verification keys) from `captures/kit.toml` `[[http]]`,
  as it does for a processor's `ctx.http`.

### Changed

- A `script` exit 65 (a processor's `Permanent`) dead-letters the batch on the first attempt; every other non-zero exit is
  still retried.
- `deploy/scripts/gmail_delta.py` and `gcal_delta.py` are removed (they are the `gmail` and `gcal` bobbins; the README of each
  lists the old variable for every setting). `hookspool.sources.gmail` and `.gcal` still import, but declare no `action`: an
  instance's `outputs` choose it. Their captures moved to the bobbins' `captures/`.
- Spool schema 2 (migration `0002_unwrap.sql` adds the two attempt columns). Stop the dispatcher and run `hookspool migrate`
  (docs/operations/upgrading.md): both services run `hookspool check` before they start and refuse a schema 1 spool, so a
  restart alone does not migrate it.
- The message for an old or missing spool now says to stop the dispatcher and run `hookspool migrate`, and points to
  `deploy/README.md`, which now links to the upgrade page; a Hermes 413 under `unwrap` says to exclude the event or raise `max_body_bytes` (one event, not a batch).
- The documentation is organised into folders (`docs/getting-started/`, `configuration/`, `bobbins/`, `operations/` and
  `architecture/`, each with an index page), and the README is a short front page that links into them. The old pages moved:
  `ARCHITECTURE.md` to `architecture/`, `CONFIGURATION.md` to `configuration/README.md`, `ADDING_A_SOURCE.md` to
  `bobbins/writing-a-bobbin.md`, `ADDING_A_DESTINATION.md` to `configuration/destinations.md`, `BOBBIN_REVIEW.md` to
  `bobbins/review.md`, `CLI.md` to `operations/cli.md`, and `OPERATIONS.md` and the body of `deploy/README.md` to pages under
  `operations/`. `docs/ARCHITECTURE.md` stays as a pointer. Each section of the README and two of the folders carry a banner
  drawing (`docs/assets/banners/`), and the configuration reference now documents `[bobbins.<instance>]`.

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
- The extension model: [docs/bobbins/writing-a-bobbin.md](docs/bobbins/writing-a-bobbin.md), [docs/configuration/destinations.md](docs/configuration/destinations.md),
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
