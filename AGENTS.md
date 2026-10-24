# AGENTS.md

hookspool is a durable, coalescing intake for inbound webhooks. It verifies a request, writes it to a SQLite spool before
the sender sees a 2xx, groups events with the same key into one batch, and delivers each batch to a script, an HTTP
endpoint or an agent gateway with retries. The core is Python 3.11 standard library only, with no LLM and no network at
import time or in tests.

This file has two parts. Read the one that matches your task:

- [Working on this repository](#part-1-working-on-this-repository): changing code, docs or a bobbin.
- [Operating hookspool for a user](#part-2-operating-hookspool-for-a-user): installing it, turning on a service, keeping it healthy.

Each built-in bobbin also has its own `AGENTS.md` with the setup steps for that one service. Link to it; do not copy it.

## Part 1: Working on this repository

### Repo map

| path | what is there |
|---|---|
| `hookspool/` | the package. Core modules below; nothing in here is specific to one sender except inside `bobbins/` |
| `hookspool/__main__.py` | the CLI (`hookspool <command>`) |
| `hookspool/intake.py`, `hookspool/dispatch.py` | the two processes: the HTTP intake, and the single deliverer |
| `hookspool/store.py`, `hookspool/migrations/` | the SQLite spool and its schema |
| `hookspool/config.py`, `hookspool/bobbin_loader.py` | config parsing and checks; bobbin discovery and expansion into ordinary tables |
| `hookspool/verify.py`, `hookspool/plugin.py`, `hookspool/net.py`, `hookspool/feeds.py` | signature checks; the source plugin API; outbound HTTP, OAuth, secret access and request pacing; Atom and RSS parsing |
| `hookspool/processor.py`, `hookspool/breaker.py`, `hookspool/logs.py` | the bobbin processor runtime; the circuit breaker; structured logs |
| `hookspool/bobbins/<vendor>/<product>/` | one built-in bobbin per directory (manifest, captures, README, SETUP.md, AGENTS.md) |
| `hookspool/providers/`, `hookspool/mailrules.py` | code shared by several bobbins, written once on core APIs: one provider's helpers; mail rules and rule packs |
| `hookspool/sources/`, `hookspool/destinations/` | source and destination plugin registries and the built-ins |
| `hookspool/testing/bobbinkit.py` | the bobbin conformance kit behind `hookspool bobbin check` |
| `tests/` | the hermetic suite; `tests/bobbins/test_<vendor>_<product>.py` holds a bobbin's own tests |
| `docs/` | `getting-started/`, `configuration/`, `bobbins/`, `operations/`, `architecture/` |
| `deploy/` | systemd units, nginx example, a complete example config, sweeps |
| `.github/` | CI workflows, `.github/scripts/plugin_ci.py`, the pull request template, `.github/core-allowlist.txt` |

### Setup

Python 3.11 or newer.

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
```

### Check your work

Run the narrowest thing that covers your change while you iterate, and the whole suite once before you finish.

```sh
python -m pytest -q tests/test_docs.py                  # one file while iterating
python -m pytest -q tests                               # the whole suite, about a minute
ruff check .
hookspool bobbin check hookspool/bobbins/<vendor>/<product> --base main    # the scorecard for one bobbin
python .github/scripts/plugin_ci.py all --base main     # what CI runs for a pull request
```

Do not run the full suite in the background or in a loop. It is hermetic: stub servers on ephemeral ports, a fake clock,
temporary directories, test secrets only. Keep it that way.

### Rules

The contract is [ARCHITECTURE section 14.3](docs/architecture/ARCHITECTURE.md#143-bobbins-service-plugins). When this file and
that section disagree, that section wins. [CONTRIBUTING.md](CONTRIBUTING.md) has the full rules for changing the core.

- One bobbin per service, named `<vendor>.<product>` (exactly one dot): `google.gmail`, `atlassian.trello`.
  Never combine services. The `hookspool.` namespace is reserved for generic bobbins shipped in core.
- A bobbin never imports or reads another bobbin, a sibling under the same vendor included. Code two bobbins would share
  goes into core (`hookspool/net.py`, `hookspool/verify.py`, `hookspool/plugin.py`, `hookspool/providers/`), never into a
  bobbin. The table of what a bobbin must call, and the list of imports and calls it may not use, is
  [14.3.7](docs/architecture/ARCHITECTURE.md#1437-bobbins-call-core-they-never-re-implement-it-and-never-each-other).
- Every bobbin has a `[provider]` table in `bobbin.toml`, and `README.md`, `SETUP.md` and `AGENTS.md` that open with the
  same frontmatter ([14.3.2](docs/architecture/ARCHITECTURE.md#1432-manifest-bobbintoml),
  [14.3.8](docs/architecture/ARCHITECTURE.md#1438-setup-instructions-setupmd-agentsmd)).
- The core has no per-source code. Do not add `if source == ...` to `store.py`, `intake.py`, `dispatch.py`, `config.py` or
  the destinations. If a plugin cannot do what it needs, extend the plugin API and say so in the pull request.
- Compare signatures, tokens and digests only through `hookspool.verify`, never with `==`.
- Never log or print secrets, headers, query strings or payload bodies. Logs carry ids, names, sizes, states, codes and
  durations. Secrets are file or environment references in config; a test value is the only secret a test may hold.
- Public repository: no hostnames, addresses, account names, personal names, messaging identifiers or real secrets in code,
  tests, captures, docs or examples. Use `example.com`, `example.test`, `127.0.0.1` and obvious placeholders. Real values
  belong in `deploy/local/`, which is git-ignored. A capture is a made-up payload signed with a test secret.
- Update the docs your change touches. `tests/test_docs.py` fails when a command, option or config key in the code is missing
  from the docs or the other way round.

### Invariants you must not break

The reasons are in [ARCHITECTURE](docs/architecture/ARCHITECTURE.md), and the test suite checks each one.

- Commit before acknowledge: a request is verified and committed to SQLite before the sender sees a 2xx, or it gets a 503
  (sections [2](docs/architecture/ARCHITECTURE.md#2-components-and-process-model) and
  [9](docs/architecture/ARCHITECTURE.md#9-intake-backpressure-and-quarantine)).
- Verify first: body cap, then signature, then anything else of a bobbin's. A handshake is answered only after the signature
  verifies (section 10).
- At-least-once delivery with order per key; a batch is retried until the destination accepts it (section 6).
- A bobbin that raises after verification quarantines the request; it never answers 5xx for our bug. Do not swallow errors
  to avoid that.
- No undelivered batch is discarded automatically: a `dead` batch stays until an operator replays it or runs `hookspool drop`.
  Retention prunes only delivered and dropped data ([section 8](docs/architecture/ARCHITECTURE.md#8-retention)).
- Only the dispatcher delivers, and only one dispatcher runs (the `dispatch.lock` file).

### Add a bobbin

1. Scaffold it: `hookspool bobbin new <vendor>.<product> --dir hookspool/bobbins --set scheme=hmac-sha256-hex --set header=X-Signature --set key=id`.
   Use `--code` for a Python source and `--processor` for post-commit work.
2. Fill in every TODO: the `[provider]` pages, secrets, settings, README.md, SETUP.md and AGENTS.md, and the two captures
   (an accepted one and a rejected one), copied from a real delivery with the values replaced by test values.
3. Add `tests/bobbins/test_<vendor>_<product>.py` for anything beyond the kit's own checks.
4. Run `hookspool bobbin check hookspool/bobbins/<vendor>/<product> --base main` until every required row is green.
5. Run `python .github/scripts/plugin_ci.py all --base main`, then the pull request template's boxes.

Read [Writing a bobbin](docs/bobbins/writing-a-bobbin.md) before step 1 and [Reviewing a bobbin](docs/bobbins/review.md) before
step 5. If the sender's signature scheme is one the template source supports, use the config-template route, which needs no Python.

### Pull requests

- A bobbin-only pull request changes only `hookspool/bobbins/<vendor>/<product>/`, its own test file, and a new vendor's
  empty `__init__.py`. Leave `CHANGELOG.md`, `README.md` and the catalogue in `docs/bobbins/README.md` alone; the maintainer
  regenerates them on merge.
- Anything else is a core change and is reviewed as one. Keep it to one thing, write the test with it (a bug fix starts with
  a failing test), and add a `CHANGELOG.md` line.
- Commit messages: a short imperative summary line, then a paragraph on why when it is not obvious.

## Part 2: Operating hookspool for a user

You are helping a person run hookspool on a host or turn on a service. Look before you change anything, and change only what a step below says.

### Install

- Local trial: [Install](docs/getting-started/install.md), then [Quick start](docs/getting-started/quick-start.md).
- A server with systemd and a reverse proxy: [Running hookspool](docs/operations/running.md). The unit files and an example
  config are in [deploy/](deploy/README.md).
- Installing a package or a unit file changes the host: say what you will run and get the person's OK first.

### Where things live

- The state directory holds the config file, the spool and a lock file. It is `--home`, then `$HOOKSPOOL_HOME`, then
  `[spool] home` in the config, then `/var/lib/hookspool`.
- The config is `<home>/config.toml`, unless `--config` or `$HOOKSPOOL_CONFIG` says otherwise. Every key is in
  [Configuration](docs/configuration/README.md); every command and option is in the [CLI reference](docs/operations/cli.md).
- Secrets are never in the config. The config holds references to files or environment variables.

### Turn on a service

1. Find the bobbin: `hookspool bobbin list`, then `hookspool bobbin show <vendor>.<product>`. If none fits, the generic
   `hookspool.webhook` bobbin covers any service that signs a JSON POST ([guide](docs/bobbins/webhook.md)).
2. Open `hookspool/bobbins/<vendor>/<product>/AGENTS.md` and follow it. Read its **Needs from the human** list first and ask
   the person for those before you start. A step marked `outward` changes something at the provider or on a shared account:
   ask, and wait for a clear yes, before running it.
3. Add the `[bobbins.<instance>]` table as the bobbin's steps say, then `hookspool bobbin expand <instance>` to see what it
   becomes.
4. `hookspool check`. Fix what it reports in the config, not in the bobbin. Only when it is clean, `hookspool reload`.
5. Verify with the steps in the bobbin's AGENTS.md, and with `hookspool status`, `hookspool list --source <instance>` and
   `hookspool issues`. [Verifying](docs/getting-started/verifying.md) says what healthy looks like.

### Moving a Hermes setup to hookspool

For senders posting to a Hermes gateway's routes, with no sender or prompt change: [guide](docs/getting-started/from-hermes-webhooks.md).

**Needs from the human:** which routes to move (`hermes webhook list`), the gateway user's absolute path to its
`webhook_subscriptions.json`, reverse proxy access, each sender's secret in a file owned by the service user (the person
writes it), and a clear yes for step 5 (`outward`: it changes what the proxy forwards).

Run `hookspool` as the service user ([Running](docs/operations/running.md)), never root (root reads files the service
cannot). If a check fails, fix that step before the next.

1. Set `[hermes] base_url` (the gateway's listener) and `subscriptions_path` (the gateway user's absolute path, not `~`).
   **Check:** `sudo -u hookspool -- test -r <path>`; if it fails, make it readable or run the units as the gateway user.
2. Per route, add `[bobbins.<instance>]` with the sender's bobbin, the secret file, `span = "immediate"` and
   `outputs = { "*" = "hermes:<route>" }`, and a `[destinations."hermes:<route>"]` table with `unwrap = true`.
   **Check:** `hookspool bobbin expand <instance>` shows the secret as a reference only.
3. `hookspool check`, then `hookspool reload`. **Check:** `ok`; a route error means it is not in the file at that path.
4. Send a signed test request to loopback `/hooks/<instance>`, as the guide shows (it reaches the real route: say so
   first). **Check:** `hookspool list --source <instance>` shows it `delivered`.
5. **Outward:** the person points the route's proxy location at `/hooks/<instance>`. **Check:** a real delivery appears
   in `hookspool list --source <instance>`; if not, roll back at once.
6. For a sender that does not retry (GitHub), install its redelivery sweep ([Sweeps](docs/operations/sweeps.md)). **Check:**
   one `--dry-run` exits 0 and prints its summary line (exit 3: `gh` failed). Bodies over the source `max_body_bytes` now get 413 at the intake.

**Rollback:** the person points the proxy location back at the gateway. Keep the instance and its destination table until
`hookspool list --source <instance>` shows nothing open, sealed, leased or retry; only then remove them, then
`hookspool check` and `hookspool reload` (earlier, leftover batches go out as batch documents to a route that still
filters). Coalescing, tee routes and bridges come later; the guide gives the order for dropping `unwrap`.

### Day two

- `hookspool status` is the one-screen health view; `hookspool list --state dead` finds stuck batches; `hookspool issues`
  lists open problems. The rest, including `replay`, `pause` and `export`, is in [Day-two commands](docs/operations/day-two.md),
  [Health and repair](docs/operations/health-and-repair.md) and [Sweeps](docs/operations/sweeps.md).
- `hookspool drop` discards a batch for good, and `hookspool pause` stops a destination. Do either only when the person
  asked for that batch or destination by name.
- Before an upgrade, read [Upgrading](docs/operations/upgrading.md). Order matters: stop the dispatcher, `hookspool migrate`,
  start it, restart the intake.

### Never

- Never print, log, copy or move a secret's value. The person writes each secret file; you only point the config at it.
- Never edit a built-in bobbin on a host. Change behaviour with settings, a hooks module, or a private bobbin in a
  directory listed in `[spool] bobbin_path`: [Private bobbins and hooks](docs/bobbins/private-bobbins.md).
- Never paste a request body into a chat, an issue or a log. Redact it first.
