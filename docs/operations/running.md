# Running hookspool

How to put hookspool on a host and move senders onto it one at a time. The files named here are in [`deploy/`](../../deploy/).

## Install

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
4. **Bobbins and scripts.** Gmail, Calendar and Drive are the built-in `google.gmail`, `google.calendar` and `google.drive` bobbins: nothing to copy.
   Each instance (`[bobbins.gmail]`, `[bobbins.calendar]` in the config example) keeps its state in
   `<home>/bobbins/<instance>`, which the unit already allows; a `state_dir` you set outside the home goes in
   `ReadWritePaths` and must be private (mode 0700: it holds OAuth tokens in the clear). Each bobbin's
   `SETUP.md` lists the provider-side steps and the secrets (the OAuth credential files). Copy
   `deploy/sweeps/github_redeliver.py` to `/opt/hookspool/sweeps/` for the GitHub redelivery sweep ([Sweeps](sweeps.md)). A
   `script:` destination of your own (for the `google.calendar` `changed` output) is written like any other; to keep a
   journal in it, import `deploy/scripts/hookspool_batch.py` ([`deploy/scripts/README.md`](../../deploy/scripts/README.md)).
5. **Config.** Copy `config.example.toml` to `/var/lib/hookspool/config.toml`, replace every placeholder, then:
   ```
   sudo -u hookspool env HOOKSPOOL_HOME=/var/lib/hookspool hookspool check      # exit 2 lists what is wrong
   sudo -u hookspool env HOOKSPOOL_HOME=/var/lib/hookspool hookspool migrate    # creates the spool, schema 2
   ```
   `check` reads `hermes.subscriptions_path` and **fails on any `events` filter or `coalesce` block on a Hermes
   route that hookspool feeds**: hookspool coalesces, and a filter on the Hermes side would silently drop part of a
   batch. Remove them from the subscriptions first (keep the old file as the rollback for this step). A route whose
   `hermes:` destination has `unwrap = true` is exempt: it gets the original events one by one, so keep its filter.
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
   [Upgrading](upgrading.md), not this step.
7. **Alerts.** Replace `alerts.argv` with the chat sender (the alert text is appended as the last argument) and
   test it with `HOOKSPOOL_ALERT_DRY=1` on, then off. An alert is marked sent only when the command exits 0.
   The alert command runs inside the dispatch unit's sandbox (`ProtectSystem=strict`, `ProtectHome=read-only`),
   like the scripts: a sender that writes anything (a token cache, a session file) needs that path in the dispatch
   unit's `ReadWritePaths`, or it fails and the alert is retried.
8. **Proxy.** Copy `nginx.example.conf`, replace the hostname and certificate paths, `nginx -t && systemctl reload nginx`.
   Do not expose `/healthz` or `/metrics`; the example proxies `/hooks/*` and the GitHub paths only.
9. **Offline check of a sender's request** before pointing it at hookspool: capture one real request, replace the
   secret with a test one and run `hookspool source test <name> --request FILE` (format in [Writing a bobbin](../bobbins/writing-a-bobbin.md#capture-files)).

## Rollout order and rollback, one source at a time

Leave a source's `expect_every_s` at `0` until its sender points at hookspool, then set the real value; a fresh
install otherwise reports `gmail silent` and `calendar silent` (and alerts) for senders that were never moved.

Cut over one sender at a time. A source is "live" when the **provider-side URL** points at hookspool; the old
path (gateway or bridge) stays running and untouched until the new one has run clean for a few days and its
sweep exists. Changing a URL back is the rollback in every row; the spool keeps whatever it already accepted
and delivers it, so a rollback never strands events.

| step | do | verify | rollback |
|---|---|---|---|
| 0. install | [Install](#install), no sender pointed at it | `hookspool status` ok; a [drill](drills.md) | `systemctl disable --now` both units; delete nothing |
| 1. GitHub | set the hook's payload URL to `https://<host>/github/ci` (CI) and `/github/prs` (PRs), same secret; narrow the hook's event list in GitHub, because the route's `events` filter on the Hermes side is gone; install the redelivery sweep | GitHub's "Redeliver" on a recent delivery shows 200 `queued`; `hookspool list --source github`; the agent wakes once for a burst | set the payload URL back; restore the Hermes `events` filter |
| 2. Plaid | set the item webhook URL (`/item/webhook/update`, or the Link token's `webhook`) to `https://<host>/hooks/plaid`; keep the daily sync | the first real webhook from a production item is `queued` (`hookspool list --source plaid`); the 401 log line names no key. The example has `env = "production"`, so a sandbox `fire_webhook` is rejected by design (it is signed with sandbox keys): to rehearse with a sandbox item, run a scratch copy of the config with `env = "sandbox"` and put it back | set the URL back |
| 3. Gmail | change the Pub/Sub push subscription's endpoint to `https://<host>/hooks/gmail?token=<push token>`; the watch itself is unchanged | `hookspool list --source gmail`; `script:gmail` batches go `delivered`, state advances under `<home>/bobbins/gmail` | set the endpoint back; the old delta script's state file is untouched unless the bobbin's `state_dir` points at its directory (it does not by default) |
| 4. Calendar | re-create the watch channel with `address = https://<host>/hooks/calendar` and the channel token (a channel's address cannot be edited); stop the old channel after the first push arrives | a change shows as one `calendar` batch per calendar after the span; `sync` pings are acknowledged and not queued | re-create the channel on the old address |
| 5. Trello | register a new webhook whose `callbackURL` equals the source's `callback_url` byte for byte, then delete the old one | Trello's unsigned HEAD answered 200; an action on the board is `queued` | delete the new webhook, keep the old one |
| 6. Zoom | edit the event subscription's endpoint URL to `https://<host>/hooks/meetings` and press Validate | Validate succeeds only if the secret is right (an unsigned or mis-signed validation is a 401 by design); a test meeting event is `queued` | set the endpoint back |
| 7. retire | after a few clean days: stop and disable the old bridge for that source; remove the poll or reduce it to its [sweep](sweeps.md) | `hookspool status` shows the source's `last_event_age_s` moving | re-enable the bridge and repoint the sender |

Per-step notes:

* **Do not retire a bridge on the same day you cut over.** Both paths cost nothing while only one URL is registered.
* **Hermes prompts.** Hermes receives hookspool's batch document, not the raw payload: the original events are
  `events[]`, with `event_type`, `event_types`, `count` and `summary` beside them ([Architecture](../architecture/ARCHITECTURE.md), section 6.4).
  Update each route's prompt template to read from the batch before cutover, and test it with `hookspool enqueue`
  (see [Drills](drills.md)).
* **A reload is one command**: `systemctl reload hookspool-intake hookspool-dispatch` (or `hookspool reload`). It
  validates first; an invalid file keeps the old config and opens a `config:file` issue.
