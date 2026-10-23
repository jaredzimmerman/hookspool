# Deploying hookspool

Everything here is generic. Hostnames, secret paths, script argv and the route map of a real deployment go
in `deploy/local/` (gitignored), copied from the `*.example*` files.

| file | what it is |
|---|---|
| `hookspool-intake.service` | the loopback HTTP intake (systemd) |
| `hookspool-dispatch.service` | the single deliverer (systemd); `ReadWritePaths` lists the home and every script's state dir outside it |
| `config.example.toml` | a complete config: spans, sources, routes, destinations, alerts |
| `nginx.example.conf` | the reverse proxy: TLS, buffering, body size, `limit_req`, one location per source |
| `scripts/` | the stand-alone batch helper for scripts that keep a journal ([`scripts/README.md`](scripts/README.md)); Gmail and Calendar are bobbins now |
| `sweeps/github_redeliver.py` | the GitHub redelivery sweep |

The instructions for using these files are in [`docs/operations/`](../docs/operations/README.md):

| page | what is in it |
|---|---|
| [Running](../docs/operations/running.md) | installing on a host, and the rollout order with a rollback for every step |
| [Drills](../docs/operations/drills.md) | the gateway-down drill |
| [Sweeps](../docs/operations/sweeps.md) | which senders retry, what keeps the rest honest, and the known limits |
| [Upgrading](../docs/operations/upgrading.md) | moving a running install to a new version |
| [Day-two commands](../docs/operations/day-two.md) | the commands you reach for once it is running |

Design background is [`docs/architecture/ARCHITECTURE.md`](../docs/architecture/ARCHITECTURE.md): section 2 (processes),
section 9 (intake, which senders retry), section 12 (failure modes), section 13 (sweeps).

## Upgrading

Stop the dispatcher, run `hookspool migrate`, start the dispatcher, restart the intake. The full order, with the
commands and the rollback, is in [Upgrading](../docs/operations/upgrading.md).
