# Upgrading

How to move a running install to a new version.

A schema change ships with a migration ([Architecture](../architecture/ARCHITECTURE.md), section 17). Only the dispatcher at startup and
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
stopped for a few seconds (`systemctl stop hookspool-intake` first); the senders see 502 for that time, and [Sweeps](sweeps.md)
says which of them retry, so do it in a quiet hour. A schema newer than the code (a downgrade) is refused with exit
2: restore the older binary or the backup, never edit `meta.schema_version`. Roll back a bad upgrade by reinstalling
the previous version while the schema is unchanged; once a migration has run, restore the backup instead
([Backup and restore](health-and-repair.md#backup-and-restore)).

## Upgrading to 0.2 (bobbins)

0.2 turns the built-in services into bobbins: one `[bobbins.<instance>]` table per service in place of a hand-wired
source, script, environment file and state directory ([Bobbins](../bobbins/README.md)). Most of a 0.1 config keeps
working, since a `[sources.*]` table is still what an instance expands to. The part that breaks is anything that runs
the Gmail or Calendar delta script.

### What was removed

- `deploy/scripts/gmail_delta.py` and `deploy/scripts/gcal_delta.py`. They are now the `gmail` and `gcal` bobbins and
  their processors. A config that still has `[destinations."script:gmail_delta"]` or `[destinations."script:gcal_delta"]`
  still loads, but every batch fails when the script is started and is dead-lettered after its retries.
- The copy mode of `deploy/scripts/hookspool_batch.py`, where the delta scripts and the batch helper were copied to a
  directory of their own. The file now re-exports `hookspool.processor` and needs the interpreter hookspool is
  installed in.
- The default `action` of `hookspool.sources.gmail` and `hookspool.sources.gcal`. A hand-written `[sources.gmail]` or
  `[sources.gcal]` table still loads but must name its own `action`; an instance's `outputs` choose it instead.
- The config templates in `examples/sources/`. They are the `github`, `trello` and `kaneo` bobbins; the old files are
  one-line pointers kept for old links. A `[sources.github]` table you copied from one still works unchanged.

### Moving a service to its instance

Do it for one service at a time, when nothing is waiting for its old destination: `hookspool list --destination
script:gmail_delta` should show no `open`, `sealed` or `retry` batch. Even if one is left behind, nothing is lost: the
processor reads everything since the stored history id (Gmail) or sync token (Calendar), so the next push picks it up.

1. Replace the tables. Delete `[sources.gmail]` and `[destinations."script:gmail_delta"]` and write one instance with
   the same name, so the URL (`/hooks/gmail`) and the reverse-proxy location stay as they are:

   ```toml
   [bobbins.gmail]
   secrets.push_token = { file = "/etc/hookspool/secrets/gmail_push_token" }   # the source's secret, unchanged
   secrets.cred_owner = { file = "/etc/hookspool/google/owner@example.com.json" }
   settings = { owner = "owner@example.com", tz = "America/New_York" }
   outputs = { scheduling = "hermes:mail-scheduling" }
   state_dir = "/var/lib/hookspool/scripts/gmail"   # the old destination's state_dir: keeps the history ids
   ```

   Calendar works the same way: `[sources.gcal]` and `[destinations."script:gcal_delta"]` become `[bobbins.gcal]` with
   `secrets.channel_token`, `secrets.cred`, the settings below, `outputs` and `state_dir =
   "/var/lib/hookspool/scripts/gcal"`. A hand-wired `[sources.<name>]` for another service becomes `[bobbins.<name>]` the
   same way: its secrets move across as they are, its `action` becomes the `"*"` output, and window or expectation
   overrides (`span`, `quiet_s`, `expect_every_s`, ...) stay instance keys.
2. Keep the state: point `state_dir` at the directory the script used (its `GMAIL_DELTA_DATA` or `GCAL_DELTA_DATA`,
   else the old destination's `state_dir`). The state files keep their names (`gmail_push_state.json`,
   `gmail_push_state_agent.json`, `gcal_push_state.json`), so the history ids and the sync token carry over and the first
   pass reads only what is new. Without `state_dir` the instance starts in `<home>/services/<instance>/` and its first
   push only records where Gmail or Calendar is now. The directory must be private (mode 0700, owned by the service
   user): the processor keeps OAuth access tokens there.
3. Move the variables to settings. Take each value from the old `env_file` and put it under `settings`, or under
   `secrets` for the credentials, as the table below says. The routes JSON (`HOOKSPOOL_DELTA_ROUTES`) becomes `outputs`;
   the event types keep their names, and an output you leave out is dropped.
4. Check and switch. Run `hookspool check`, then `hookspool bobbin expand gmail` to see the `[sources.gmail]` and
   `[destinations."script:gmail"]` the instance becomes, then `hookspool reload`. Delete the old env file and the copied
   scripts once the new instance has delivered.

| old variable | now |
|---|---|
| `GMAIL_DELTA_OWNER` | setting `owner` |
| `GMAIL_DELTA_AGENT` | setting `agent` (optional; it may not be the owner's mailbox) |
| `GMAIL_DELTA_AGENT_STATE` | setting `agent_state` |
| `GMAIL_DELTA_TZ` | setting `tz` |
| `GMAIL_DELTA_HOURS` | setting `hours` |
| `GMAIL_DELTA_OWN_DOMAINS` | setting `own_domains` (a list) |
| `GMAIL_DELTA_CRED_DIR` | secrets `cred_owner` and `cred_agent`: one `file` reference each, to `<dir>/<address>.json` |
| `GMAIL_DELTA_EMAIL_CHECK` | settings `skip_domains`, `skip_keywords` and `important_senders` (lists) |
| `GMAIL_DELTA_SCHED_MODULE` | setting `hooks`: the absolute path of a module that defines `classify` ([Private bobbins and hooks](../bobbins/private-bobbins.md)) |
| `GMAIL_DELTA_API`, `GMAIL_DELTA_FREEBUSY` | settings `api_url`, `freebusy_url` (test servers only) |
| `GMAIL_DELTA_DATA` | instance key `state_dir` |
| `GCAL_DELTA_CRED` | secret `cred` |
| `GCAL_DELTA_TZ` | setting `tz` |
| `GCAL_DELTA_OWN` | setting `own` (a list) |
| `GCAL_DELTA_BRIEF_DIR` | setting `brief_dir` |
| `GCAL_DELTA_API` | setting `api_url` |
| `GCAL_DELTA_SYNC_SH` | the `changed` output, mapped to a `script:` destination of your own |
| `GCAL_DELTA_SKILL_DIR`, `GCAL_DELTA_TRAVEL_PY` | setting `hooks`: a module that defines `travel` |
| `GCAL_DELTA_DATA` | instance key `state_dir` |
| `HOOKSPOOL_DELTA_ROUTES` | instance key `outputs` |

### Verifying

- `hookspool check` prints `ok`, and `hookspool bobbin expand <instance>` shows `processor = "<instance>"` and your
  `state_dir`.
- After the next push, `hookspool list --source gmail` shows a batch delivered to `script:gmail`, and `hookspool status`
  lists `script:gmail` with its breaker closed and no `script:gmail_delta`.
- The state file in `state_dir` has a newer history id (or sync token) than before the switch.
- To roll back, restore the old tables and reinstall 0.1, which still has the scripts. The state files have the same
  names and fields, so the old script carries on from the history id or sync token the processor stored.
