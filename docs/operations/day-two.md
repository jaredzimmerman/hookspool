# Day-two commands

The commands you reach for once it is running.

## Day-two commands

```
hookspool status                     # the one-screen health view (same JSON as /healthz)
hookspool issues                     # open issues; ack / resolve / ignore <id>
hookspool list --state dead          # then: show <id> --events, split, exclude, drop
hookspool replay --dead --destination hermes:github-ci   # retry dead batches after the cause is fixed
hookspool pause hermes:github-ci --reason "gateway upgrade"   # destinations only; resume undoes it
hookspool export --since … > backup.jsonl                     # forensic copy of batches, events, attempts
```

Back up the state directory with SQLite's own copy (`sqlite3 spool.db "VACUUM INTO '…'"`) or `hookspool export`,
not by copying `spool.db` alone while the dispatcher runs. Details are in [Architecture](../architecture/ARCHITECTURE.md), sections 8, 12
and 16. Take one before an upgrade that carries a migration.
