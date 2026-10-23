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
