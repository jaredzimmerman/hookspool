# Changelog

All notable changes are recorded here. The format follows Keep a Changelog, and
the project follows Semantic Versioning once it reaches 1.0; until then a minor version may change
an interface, and the entry will say so.

## [Unreleased]

### Added

- Poll sources (ARCHITECTURE 14.4), for services with no push at all: a bobbin may declare `[poll]` (`every_s` >= 60,
  `jitter_s`, `module = "poller"`) in place of `[source]`, and its `poller.py` answers `poll(batch, ctx)` with the same
  batch, context, journal, staged state and emits as a processor. The dispatcher keeps a durable schedule in the new
  `polls` table (schema 3, `0003_polls.sql`; run `hookspool migrate`) and enqueues one tick per window
  (`{"tick", "instance"}`, key `poll`, dedup id `poll:<instance>:<scheduled next_at>`) in the same transaction as the
  `next_at` update: a missed window makes one tick, never a burst, and no tick is enqueued while the previous one is
  undelivered. A poll instance has no URL (`/hooks/<instance>` is 404) and may override `every_s` and `jitter_s`.
  `hookspool status` shows each poll under `polls`, and `poll:<instance>:stale` opens after three windows without a
  delivered tick.
- `ctx.http.get_text(url, headers)` (and on `ctx.oauth(name)`): `(status, text, response_headers)`, with a 304 returned,
  not raised, so a poller can make a conditional GET.
- `hookspool bobbin new <vendor>.<product> --poll` scaffolds a poll bobbin. The conformance kit runs `poller.py` once per
  `captures/poll-*.json` (stub API answers), and its `provider` row asks a poll bobbin for `api_docs`, `no_push_note` and
  `no_push_docs` in place of `webhook_docs` and `signature_docs`; the documents' frontmatter carries `api_docs`.
- The `google.meet` bobbin (`hookspool/bobbins/google/meet/`): Google Meet events from a Google Workspace Events
  subscription on a user, delivered through a Cloud Pub/Sub push with the token in the URL. The source keys each
  CloudEvent by its conference record and dedups on `ce-id`. The processor returns `meeting-started` for the first
  batch of a call (no API call) and `meeting-ended` once the call ends: the conference id, space, start, end, and each
  participant's display name, kind (`signedin`, `anonymous`, `phone`), user id, first join and last leave, read from
  Meet REST v2. Subscription lifecycle events become `subscription-changed`. SETUP.md and AGENTS.md cover the topic,
  the publisher grant, the numeric user id the target needs, and the daily renewal call. No transcripts.
- A `meeting` span preset (60 s quiet, 900 s max), so a short call's start, joins, leaves and end are one batch.
- `hookspool.providers.google`: `verify_push` and `push_message` (the Pub/Sub push token and envelope, now shared by
  `google.gmail` and `google.meet`), and the Meet REST v2 helpers `meet_conference`, `meet_participants`,
  `meet_sessions`, `meet_participant` and `meet_attendance`.

## [0.2.0] - 2026-10-23

### Added

- A guide for moving a Hermes gateway's webhook routes behind hookspool with no sender or prompt changes
  (`docs/getting-started/from-hermes-webhooks.md`), summarised in the README and as agent steps in `AGENTS.md`.
- The `hookspool.webhook` bobbin: any web service that sends a signed or tokened JSON POST, configured entirely by settings
  (`[source] template = "settings"`; five schemes). One rule now maps a bobbin setting named after a template key to that
  key's value, for a fixed template (Trello's `callback_url`) and for `"settings"` alike; settings may be a `table`, and an
  instance of a `"settings"` bobbin may map any event type next to `"*"`. `hookspool bobbin new <vendor>.<product>` now
  copies the `hookspool.webhook` shape and freezes its settings into a fixed template (`--set NAME=VALUE`, `--code` for a `source.py`). An
  empty setting value is unset; `check` reports an error when outputs are mapped per event type without
  `attrs.event_type`; a scaffolded `hmac-sha1-base64-url` bobbin keeps `callback_url` as a required setting.
- The `google.drive` bobbin (`hookspool/bobbins/google/drive/`): Google Drive push (a watch channel on the change feed) with a processor
  that reads `changes.list` from a stored page token and reports `file-changed`, `file-created` and `file-shared` items
  (file id, name, MIME type, modified time, actor, link; never contents). It drops the configured accounts' own edits,
  files in the trash and listed MIME types, re-bootstraps quietly when Drive rejects the page token, and replays a retried
  batch from the journal. The Drive change-feed calls live in `hookspool.providers.google` next to the channel helpers the
  `google.calendar` bobbin also uses.
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
  stand-alone copy mode of `deploy/scripts/hookspool_batch.py` is retired (see Removed).
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

- The `google.gmail` and `google.calendar` bobbins (`hookspool/bobbins/google/`): the Gmail and Calendar push sources with their delta
  logic as processors, settings instead of environment variables, one credential secret per Google account, a
  `hooks.classify` hook for private mail hand-offs, a `hooks.travel` hook for travel blocks, and the outputs
  `scheduling`, `agent-mail`, `agent-schedule`, `mailbox-changed` and `conflict`, `brief`, `changed` (the last replaces the
  script that was started on the side). `hookspool/providers/google.py` holds what the Google bobbins share: the push-channel
  check (token, `sync` handshake, header payload) and the Gmail, Calendar and free/busy calls on `ctx.oauth`.
- A bobbin secret may be marked `required = false`, so an instance can leave it out.
- The `zoom.meetings` and `plaid.items` bobbins (`hookspool/bobbins/zoom/meetings/`, `hookspool/bobbins/plaid/items/`): the Zoom event subscription and the Plaid webhook sources, moved
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
- Release tooling. `hookspool --version` prints the version. The package metadata now carries authors, keywords,
  classifiers and project links. CI gains a `build` job that builds the sdist and the wheel, runs `twine check`, and
  installs the wheel into a fresh environment to run `hookspool --version` and `hookspool bobbin list`. A `v*` tag runs
  `.github/workflows/release.yml`: it checks that the tag matches `hookspool.__version__`, builds, attaches both files
  to a GitHub Release with this file's section as the notes, and publishes to PyPI through Trusted Publishing
  ([docs/operations/releasing.md](docs/operations/releasing.md)). Every action is pinned to a commit, and Dependabot
  keeps the actions and the Python requirements current.
- A documentation site built from `docs/` with MkDocs Material (`mkdocs.yml`, `requirements-docs.txt`). Pull requests
  build it with `--strict`, and each push to `main` publishes it to GitHub Pages. A social preview image lives at
  `docs/assets/social-preview.png`.
- Community files: `SECURITY.md` (private reports through GitHub security advisories), `CODE_OF_CONDUCT.md`
  (Contributor Covenant 2.1), issue forms for bugs and new bobbins, and `.github/CODEOWNERS`.

### Changed

- Namespaced bobbin names: a bobbin is `<vendor>.<product>` (the company that runs the service, then the product or API
  whose webhooks it receives), in `hookspool/bobbins/<vendor>/<product>/` (a vendor directory holds only an empty
  `__init__.py`; shared code stays in core, e.g. `hookspool/providers/google.py`) and imported as
  `hookspool.bobbins.<vendor>.<product>`. The built-ins are `google.gmail`, `google.calendar`, `google.drive`,
  `github.repo`, `atlassian.trello`, `kaneo.projects`, `zoom.meetings`, `plaid.items` and `hookspool.webhook`; `hookspool.`
  is reserved for the generic bobbins shipped with core, and a name without exactly one dot is refused by `hookspool check`
  and `hookspool bobbin check`. No aliases for the short names. Instances stay plain identifiers (`/hooks/<instance>`) and
  must name their bobbin (`bobbin = "google.gmail"`); `check` suggests it for an instance named after the product and
  refuses a dotted `[bobbins.<vendor>.<product>]` header with the form to write. A processor's state defaults to
  `<home>/bobbins/<instance>`. `hookspool bobbin new <vendor>.<product>` writes the nested directories, a bobbin's own test
  file is `tests/bobbins/test_<vendor>_<product>.py`, and the labelling job and the kit's `scope` count a new vendor's empty
  `__init__.py` as part of the bobbin.
- Every bobbin manifest has a `[provider]` table (`vendor`, `product`, `homepage`, `webhook_docs`, `signature_docs` when
  it declares a secret, `api_docs` when its processor calls the provider's API; https pages of the provider itself), and
  its README.md, SETUP.md and AGENTS.md open with frontmatter that repeats it (`bobbin`, `provider`, `webhook_docs`). The
  scorecard has two new required rows, `name` and `provider`, and an advisory `provider-links` row that fetches each page
  with `--online` (CI passes it; it never blocks a merge). The pull request template's box for the provider's
  documentation link is gone: the `provider` row checks it.
- A `script` exit 65 (a processor's `Permanent`) dead-letters the batch on the first attempt; every other non-zero exit is
  still retried.
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

- The bobbin labels come from GitHub: `bobbin-label.yml` reads the pull request's file list through the API and applies
  the kit's `scope` rule to it. The scorecards' own scope, produced by the pull request's code, is only a cross-check; a
  disagreement labels the pull request `touches-core` and the comment says so.
- `hookspool bobbin check` runs the bobbin's tests under `python -I` with `PATH`, `LANG`, `TMPDIR` and a throwaway `HOME`
  only, as it already did for the processor.
- The bobbin catalogue in `docs/bobbins/README.md` is generated by the new `hookspool bobbin list --markdown`, and the
  README no longer keeps a second table. A pull request that adds a bobbin edits nothing outside its directory: the
  maintainer regenerates the catalogue and writes the changelog line on merge.
- The Gmail bobbin refuses an `agent` setting that names the owner's mailbox, and logs when one pass stops at the history
  page cap.
- `.github/scripts/plugin_ci.py` and the kit's `scope` check read git's file lists NUL-separated, so a path with a space
  or a non-ASCII character is one path.

### Removed

Moving a 0.1 deployment across is in [Upgrading to 0.2](docs/operations/upgrading.md#upgrading-to-02-bobbins).

- `deploy/scripts/gmail_delta.py` and `gcal_delta.py`: they are the `google.gmail` and `google.calendar` bobbins. A config that still runs
  them loads, but every batch fails at spawn; the upgrade page maps each old variable to its setting.
- The stand-alone copy mode of `deploy/scripts/hookspool_batch.py` (it re-exports `hookspool.processor`).
- The default `action` of `hookspool.sources.gmail` and `.gcal`; they still import, and an instance's `outputs` choose
  the destination. Their captures moved to the bobbins' `captures/`.
- The config templates in `examples/sources/` (now the `github.repo`, `atlassian.trello` and `kaneo.projects` bobbins; the files are pointers),
  and the contributor path built on them (`tests/captures/<name>/`, `tests/test_sources_template.py --write`). A new
  bobbin starts from `hookspool bobbin new` and is checked by `hookspool bobbin check`.

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

[Unreleased]: https://github.com/jaredzimmerman/hookspool/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/jaredzimmerman/hookspool/releases/tag/v0.2.0
