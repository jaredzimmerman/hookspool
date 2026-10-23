# Operations

<picture><source media="(prefers-color-scheme: dark)" srcset="../assets/banners/breakers-dark.svg"><img alt="A panel of circuit breakers in a row, one of them tripped." src="../assets/banners/breakers-light.svg" width="960"></picture>

How to run hookspool on a host, keep it healthy and fix what goes wrong. Nothing here needs the source code.

| page | what is in it |
|---|---|
| [Running](running.md) | installing on a host with systemd and a reverse proxy, and moving senders over one at a time with a rollback for each |
| [Health and repair](health-and-repair.md) | telling that it is healthy, issues and alerts, stuck batches, retention, backup and restore |
| [Sweeps](sweeps.md) | which senders retry, what keeps the rest honest, and the known limits |
| [Drills](drills.md) | the gateway-down drill |
| [Upgrading](upgrading.md) | moving a running install to a new version |
| [Day-two commands](day-two.md) | the commands you reach for once it is running |
| [CLI reference](cli.md) | every command, option, environment variable and exit code |

The files these pages talk about (systemd units, an nginx example, a complete config and the GitHub redelivery sweep) are in
[`deploy/`](../../deploy/README.md).
