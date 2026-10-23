# Deploying hookspool

Everything here is generic. Hostnames, secret paths, script argv and the route map of a real deployment go
in `deploy/local/` (gitignored), copied from the `*.example*` files.

| file | what it is |
|---|---|
| `hookspool-intake.service` | the loopback HTTP intake (systemd) |
| `hookspool-dispatch.service` | the single deliverer (systemd); `ReadWritePaths` lists the home and every script's state dir outside it |
| `config.example.toml` | a complete config: spans, sources, routes, destinations, alerts |
| `nginx.example.conf` | the reverse proxy: TLS, buffering, body size, `limit_req`, one location per source |
| `scripts/` | the stand-alone batch helper for scripts that keep a journal (`scripts/README.md`); Gmail and Calendar are bobbins now |
| `sweeps/github_redeliver.py` | the GitHub redelivery sweep |

Design background is `docs/ARCHITECTURE.md`: section 2 (processes), section 9 (intake, which senders retry),
section 12 (failure modes), section 13 (sweeps).

## 1. Install

Order matters only where noted. Nothing here touches the gateway.

1. **Package.** `pip install .` into a venv (or `pipx install .`) and make the `hookspool` command available at
   `/usr/local/bin/hookspool` (the units call that path; edit them if yours differs, e.g. to `python -m hookspool`). Python 3.11+, with
   `cryptography` and `PyJWT` for the Plaid source.
2. **Account and directories.** A dedicated user is best. Create every directory that will appear in
   `ReadWritePaths` before the dispatch unit starts, or systemd refuses it with status 226/NAMESPACE.
   ```
   useradd --system --home /var/lib/hookspool --shell /usr/sbin/nologin hookspool
   install -d -m 0700 -o hookspool -g hookspool /var/lib/hookspool
   install -d -m 0750 -o root -g hookspool /etc/hookspool /etc/hookspool/secrets
   ```
   If the agent runs as root on the same host and its `webhook_subscriptions.json` is only readable by root,
   run the units as that user instead (the intake and dispatcher read that file on every attempt).
3. **Secrets.** One file per secret, mode 0600, owned by the service user. Reuse the secret the sender
   already has where the provider keeps one (a GitHub hook secret, a Zoom Secret Token): then rolling a sender back needs
   no secret change. `hookspool check` warns about files readable by group or other. Env refs (`{ env = "…" }`) do not rotate
   without a restart; prefer `file` or `env_file`.
4. **Bobbins and scripts.** Gmail and Calendar are the built-in `gmail` and `gcal` bobbins: nothing to copy.
   Each instance (`[bobbins.gmail]`, `[bobbins.gcal]` in the config example) keeps its state in
   `<home>/services/<instance>`, which the unit already allows; a `state_dir` you set outside the home goes in
   `ReadWritePaths` and must be private (mode 0700: it holds OAuth tokens in the clear). Each bobbin's
   `SETUP.md` lists the provider-side steps and the secrets (the OAuth credential files). Copy
   `deploy/sweeps/github_redeliver.py` to `/opt/hookspool/sweeps/` for the GitHub redelivery sweep (section 4). A
   `script:` destination of your own (for the gcal `changed` output) is written like any other; to keep a
   journal in it, import `deploy/scripts/hookspool_batch.py` (`scripts/README.md`).
5. **Config.** Copy `config.example.toml` to `/var/lib/hookspool/config.toml`, replace every placeholder, then:
   ```
   sudo -u hookspool env HOOKSPOOL_HOME=/var/lib/hookspool hookspool check      # exit 2 lists what is wrong
   sudo -u hookspool env HOOKSPOOL_HOME=/var/lib/hookspool hookspool migrate    # creates the spool, schema 2
   ```
   `check` reads `hermes.subscriptions_path` and **fails on any `events` filter or `coalesce` block on a Hermes
   route that hookspool feeds**: hookspool coalesces, and a filter on the Hermes side would silently drop part of a
   batch. Remove them from the subscriptions first (keep the old file as the rollback for this step).
6. **Units.** Copy both `.service` files to `/etc/systemd/system/`. Every `state_dir` of a `script:` destination
   in the config that is outside the home must appear in the dispatch unit's `ReadWritePaths`; so must any other
   directory a script writes.
   ```
   systemctl daemon-reload
   systemctl enable --now hookspool-dispatch hookspool-intake
   hookspool status              # ok: true, dispatcher alive, no batches
   curl -s http://127.0.0.1:8650/healthz
   ```
   `migrate` (step 5) created the spool; both units run `hookspool check` before they start, and it exits 2 if the
   schema is older than the code. `check` does not create or require the spool file: if it is missing, `check`
   passes and the intake refuses to start (it only reads the version and never migrates), so run step 5 first. After a package upgrade, follow
   section 5, not this step.
7. **Alerts.** Replace `alerts.argv` with the chat sender (the alert text is appended as the last argument) and
   test it with `HOOKSPOOL_ALERT_DRY=1` on, then off. An alert is marked sent only when the command exits 0.
   The alert command runs inside the dispatch unit's sandbox (`ProtectSystem=strict`, `ProtectHome=read-only`),
   like the scripts: a sender that writes anything (a token cache, a session file) needs that path in the dispatch
   unit's `ReadWritePaths`, or it fails and the alert is retried.
8. **Proxy.** Copy `nginx.example.conf`, replace the hostname and certificate paths, `nginx -t && systemctl reload nginx`.
   Do not expose `/healthz` or `/metrics`; the example proxies `/hooks/*` and the GitHub paths only.
9. **Offline check of a sender's request** before pointing it at hookspool: capture one real request, replace the
   secret with a test one and run `hookspool source test <name> --request FILE` (format in `docs/ADDING_A_SOURCE.md`).

## 2. Rollout order and rollback, one source at a time

Leave a source's `expect_every_s` at `0` until its sender points at hookspool, then set the real value; a fresh
install otherwise reports `gmail silent` and `gcal silent` (and alerts) for senders that were never moved.

Cut over one sender at a time. A source is "live" when the **provider-side URL** points at hookspool; the old
path (gateway or bridge) stays running and untouched until the new one has run clean for a few days and its
sweep exists. Changing a URL back is the rollback in every row; the spool keeps whatever it already accepted
and delivers it, so a rollback never strands events.

| step | do | verify | rollback |
|---|---|---|---|
| 0. install | section 1, no sender pointed at it | `hookspool status` ok; a drill (section 3) | `systemctl disable --now` both units; delete nothing |
| 1. GitHub | set the hook's payload URL to `https://<host>/github/ci` (CI) and `/github/prs` (PRs), same secret; narrow the hook's event list in GitHub, because the route's `events` filter on the Hermes side is gone; install the redelivery sweep | GitHub's "Redeliver" on a recent delivery shows 200 `queued`; `hookspool list --source github`; the agent wakes once for a burst | set the payload URL back; restore the Hermes `events` filter |
| 2. Plaid | set the item webhook URL (`/item/webhook/update`, or the Link token's `webhook`) to `https://<host>/hooks/plaid`; keep the daily sync | the first real webhook from a production item is `queued` (`hookspool list --source plaid`); the 401 log line names no key. The example has `env = "production"`, so a sandbox `fire_webhook` is rejected by design (it is signed with sandbox keys): to rehearse with a sandbox item, run a scratch copy of the config with `env = "sandbox"` and put it back | set the URL back |
| 3. Gmail | change the Pub/Sub push subscription's endpoint to `https://<host>/hooks/gmail?token=<push token>`; the watch itself is unchanged | `hookspool list --source gmail`; `script:gmail` batches go `delivered`, state advances under `<home>/services/gmail` | set the endpoint back; the old delta script's state file is untouched unless the bobbin's `state_dir` points at its directory (it does not by default) |
| 4. Calendar | re-create the watch channel with `address = https://<host>/hooks/gcal` and the channel token (a channel's address cannot be edited); stop the old channel after the first push arrives | a change shows as one `gcal` batch per calendar after the span; `sync` pings are acknowledged and not queued | re-create the channel on the old address |
| 5. Trello | register a new webhook whose `callbackURL` equals the source's `callback_url` byte for byte, then delete the old one | Trello's unsigned HEAD answered 200; an action on the board is `queued` | delete the new webhook, keep the old one |
| 6. Zoom | edit the event subscription's endpoint URL to `https://<host>/hooks/zoom` and press Validate | Validate succeeds only if the secret is right (an unsigned or mis-signed validation is a 401 by design); a test meeting event is `queued` | set the endpoint back |
| 7. retire | after a few clean days: stop and disable the old bridge for that source; remove the poll or reduce it to its sweep (section 4) | `hookspool status` shows the source's `last_event_age_s` moving | re-enable the bridge and repoint the sender |

Per-step notes:

* **Do not retire a bridge on the same day you cut over.** Both paths cost nothing while only one URL is registered.
* **Hermes prompts.** Hermes receives hookspool's batch document, not the raw payload: the original events are
  `events[]`, with `event_type`, `event_types`, `count` and `summary` beside them (`docs/ARCHITECTURE.md` section 6.4).
  Update each route's prompt template to read from the batch before cutover, and test it with `hookspool enqueue`
  (section 3).
* **A reload is one command**: `systemctl reload hookspool-intake hookspool-dispatch` (or `hookspool reload`). It
  validates first; an invalid file keeps the old config and opens a `config:file` issue.

## 3. Gateway-down drill

Run it after step 0, and again before you rely on it. It proves the three claims that matter: the intake keeps
answering while the destination is down, nothing grows without bound, and everything is delivered, in order, after.
It does not restart or stop the gateway: the destination is made unreachable from hookspool's side, which the
dispatcher cannot tell from a real outage.

```
export HOOKSPOOL_HOME=/var/lib/hookspool
# (set `dry = true` under [alerts] first if you do not want the chat woken; the alert is then only recorded)

# 1. baseline
hookspool status                                   # breaker closed for hermes:*, nothing pending

# 2. take the destination away: point hermes.base_url at a closed port, then reload
sed -i 's#^base_url = .*#base_url = "http://127.0.0.1:9"#' $HOOKSPOOL_HOME/config.toml
hookspool reload

# 3. feed it. Use real traffic, or inject into the dedicated drill route (same validation as the intake; one
#    batch per key, so several keys). hermes:drill needs a Hermes subscription named "drill" that does nothing
#    visible (see below); never inject drill payloads into a real route.
for i in 1 2 3 4 5 6; do
  echo "{\"drill\": $i}" | hookspool enqueue --source drill --key drill-$i --destination hermes:drill --dedup-id drill-$i
done

# 4. watch (give it two minutes: the breaker trips on the third consecutive transport failure)
hookspool status       # hermes:drill breaker: open after 3 transport failures; oldest_pending_age_s grows
hookspool list         # batches sealed or in retry, none dead; no batch exceeds [coalesce] max_events / max_bytes
hookspool issues       # exactly one delivery issue for hermes:drill, one alert (dry: recorded, not sent)
curl -s http://127.0.0.1:8650/healthz   # 200, degraded lists the open breaker. The intake is still taking events.

# 5. restore
sed -i 's#^base_url = .*#base_url = "http://127.0.0.1:8644"#' $HOOKSPOOL_HOME/config.toml
hookspool reload

# 6. confirm recovery (the next probe is within the breaker cooldown, at most 10 minutes)
hookspool status       # breaker closed; the backlog drains FIFO per key
hookspool list --state delivered
hookspool issues       # the issue auto-resolved
```

Pass criteria: nothing `dead`; every injected event appears in a `delivered` batch; the issue opened once and
resolved itself; no batch grew past its bounds while the breaker was open. To drill with the real gateway, stop it
for a few minutes instead of step 2; that is the same test with the real thing, and the one to run before trusting
an upgrade window.

**The drill route.** Add a `drill` entry to the Hermes subscriptions file (the one `hermes.subscriptions_path`
names) with a prompt that does nothing visible and `deliver` set to `log` (so no chat is messaged), a secret of its
own, and no `events` filter. The restore step delivers the six `{"drill": n}` payloads to it, and only to it: no real
agent is woken with drill data. Real traffic that arrives while the destination is away is held the same way and is
delivered after step 5, in order; that is the point of the drill, not a loss. If the drill leaves rows behind,
they are test rows: `hookspool drop <batch>… --yes` for any still pending.

## 4. Sweeps: which senders retry, and what keeps the rest honest

A webhook replaces a poll but never fully (`docs/ARCHITECTURE.md` section 13). Every migrated job keeps a sweep
that is cheap (at most two runs a day), idempotent with the push path, and replaced by `expect_every_s` where the
push covers it completely. If a source's `expect_every_s` passes with no event, a `source_silent` issue opens: that
is the signal that the push path broke and the sweep is carrying the load.

| source | old job (generic) | sweep | cadence | `expect_every_s` | sender retries a failed delivery? | script state paths |
|---|---|---|---|---|---|---|
| GitHub | none (the gateway took the POST directly, no buffer) | `sweeps/github_redeliver.py` per hook: lists the hook's deliveries, redelivers those that never got a 2xx | twice a day | `0` (or a few days on a busy repo) | **No.** Redelivery is manual (UI or `POST …/deliveries/{id}/attempts`). A 503, 429 or timeout is a loss until the sweep runs (a 413 is a loss the sweep cannot fix; see the first limit below) | none |
| Plaid | the daily transaction sync | keep the daily sync as is (it reconciles by cursor); the webhook only tells the agent sooner | once a day | `0`: item activity is irregular | Yes, with backoff for hours | none (the sync's own paths stay as they are) |
| Gmail (Pub/Sub) | the mail-arrival poll | renew the mailbox `users.watch` before its 7-day expiry (a cheap `users.watch` call) and let the `gmail` bobbin reconcile from its stored history id | once a day | `345600` (4 days) | Yes, for the subscription's retention (days) | `<home>/services/gmail` |
| Calendar | the calendar poll plus an in-memory debounce | renew the watch channel before it expires (channels last at most a week; Google does not warn); the `gcal` bobbin reconciles from its sync token | once a day | `172800` (2 days) | Yes, with backoff for a limited time | `<home>/services/gcal` |
| Trello | the board poll | list the app's webhooks; re-create any missing or `active: false` (Trello disables a webhook that keeps failing) | once a day | `0` (boards are quiet) | A few times, then may disable the webhook | none |
| Zoom | the meeting poll | none for events; check the subscription still exists | on demand | `0` | Yes, a few times over minutes | none |
| anything else | – | state it when the source is added (`docs/ADDING_A_SOURCE.md`) | – | – | assume **no** until shown | – |

Rules that make this honest:

* A sweep that finds work the push missed should **go through hookspool** (`hookspool enqueue`, or a `script:` emit),
  so it gets the same coalescing, dedup and audit trail.
* A sweep must not re-announce what the push already delivered; use the same `dedup_id` the source would.
* Every script `state_dir` is in the dispatch unit's `ReadWritePaths`. If a sweep runs under cron as a different
  user, give that user the same paths.
* The cron line for the GitHub sweep, per hook:
  `17 3,15 * * * /usr/bin/python3 /opt/hookspool/sweeps/github_redeliver.py --repo OWNER/REPO --hook-id 123456 >> /var/log/hookspool-sweep.log 2>&1`
  (with `GH_TOKEN` in the environment; fine-grained token, repository webhooks read and write, nothing else).

### Known limits, stated plainly

| limit | what happens | what to do |
|---|---|---|
| **GitHub payload over the source `max_body_bytes`** (768 KiB by default: the route's `max_bytes`, 786432, caps it; large pushes, long PR bodies, many commits) | The request gets **413** before any plugin code runs. `[intake] max_body_bytes` and nginx's `client_max_body_size` are 1 MiB, so a body between 768 KiB and 1 MiB passes nginx and is refused by the intake; above 1 MiB nginx refuses it. GitHub does not retry it, so it is not delivered. The redelivery sweep does not redeliver it (it would get the same 413) and counts it in its summary line. | Raise the limit end to end, in this order: the destination (`hermes.max_body_bytes`, and the gateway's own limit), the route `max_bytes` (at most 0.75 × the destination), the source `max_body_bytes` (at most the route's), `[intake] max_body_bytes`, nginx `client_max_body_size`. `hookspool check` enforces the order. GitHub itself caps payloads at 25 MB. Deliveries the sweep skipped for a 413 stay undelivered after you raise the limit: redeliver those once by hand (the hook's Recent Deliveries page, or `POST …/deliveries/{id}/attempts`). Watch `hookspool_requests_rejected_total{reason="too_large"}`. |
| **Plaid key-fetch limit** | A `kid` that is not cached (a key rotation, or the first key after an intake restart) shares **one key fetch a minute** per Plaid environment. A client sending random-`kid` requests about once a second can hold such a key at 401 until it stops, or until Plaid stops retrying. Known keys are cached for 24 h and a known key is still served if a refresh is blocked, so this concerns only a key not seen yet. | The nginx `limit_req` zone for `/hooks/plaid` slows an abusive address (and Plaid retries over hours, so a genuine rotation gets in once the slot frees). A small token bucket for fetches is a later option and is not built. Watch `hookspool_requests_rejected_total{source="plaid",reason="unverified"}` for a spike. |
| **Gmail `messageId` is per push** | If the intake is killed after commit and before answering, Pub/Sub redelivers with a new id and the batch holds two events. | Harmless: the `gmail` processor reads Gmail history once per batch. |
| **Pub/Sub token in the URL** | The weakest verifier of the set. It is in the subscription config, and anything that records the request line records it. nginx does this in more places than the access log: its error log writes the request line and the upstream URI (query string included) on every `limit_req` rejection and every 502 while the intake restarts. | The example keeps it out of nginx's logs: the access log format writes `$uri` only, the port-80 server has `access_log off`, and `location = /hooks/gmail` has its own `error_log … crit` with `limit_req_log_level warn`, so those lines are not written (the access log still shows a 429 or 502 for the path). **What remains:** the subscription config in Google Cloud; any proxy, CDN or load balancer in front of nginx and its logs; the `crit`/`emerg` lines nginx writes for that location (rare: out of memory, for example); and nginx's `info` or `debug` error levels if you set them on the main `error_log`, so do not (debug logging of a webhook host exposes every URL). If a log is ever found to hold a token, rotate it: write a new push token to the secret file, then update the subscription's endpoint (only one token is accepted at a time, so pushes in between get 401 and Pub/Sub retries them). OIDC verification of the push is a noted later hardening (ARCHITECTURE section 10). |
| **Loopback bind** | The intake is reachable only through the proxy. | Do not bind it to a public address. |

## 5. Upgrading

A schema change ships with a migration (`docs/ARCHITECTURE.md` section 17). Only the dispatcher at startup and
`hookspool migrate` apply one, and both services run `hookspool check` first, which exits 2 on an older schema. So
after a package upgrade the dispatcher does **not** fix the schema by restarting: it fails its `ExecStartPre` and
systemd retries every 2 seconds. Do it in this order:

```
pip install --upgrade .                                    # or pipx upgrade; the new code is on disk, the old process still runs
systemctl stop hookspool-dispatch                          # releases dispatch.lock
sudo -u hookspool env HOOKSPOOL_HOME=/var/lib/hookspool hookspool migrate   # add --dry-run first to list what is pending
systemctl start hookspool-dispatch
systemctl restart hookspool-intake                         # picks up the new code
hookspool status                                           # ok: true
```

The intake may keep running during an additive migration. A migration that is not additive needs both services
stopped for a few seconds (`systemctl stop hookspool-intake` first); the senders see 502 for that time, and section 4
says which of them retry, so do it in a quiet hour. A schema newer than the code (a downgrade) is refused with exit
2: restore the older binary or the backup, never edit `meta.schema_version`. Roll back a bad upgrade by reinstalling
the previous version while the schema is unchanged; once a migration has run, restore the backup instead
(section 6).

## 6. Day-two commands

```
hookspool status                     # the one-screen health view (same JSON as /healthz)
hookspool issues                     # open issues; ack / resolve / ignore <id>
hookspool list --state dead          # then: show <id> --events, split, exclude, drop
hookspool replay --dead --destination hermes:github-ci   # retry dead batches after the cause is fixed
hookspool pause hermes:github-ci --reason "gateway upgrade"   # destinations only; resume undoes it
hookspool export --since … > backup.jsonl                     # forensic copy of batches, events, attempts
```

Back up the state directory with SQLite's own copy (`sqlite3 spool.db "VACUUM INTO '…'"`) or `hookspool export`,
not by copying `spool.db` alone while the dispatcher runs. Details are in `docs/ARCHITECTURE.md` sections 8, 12
and 16. Take one before an upgrade that carries a migration.
