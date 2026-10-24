# Security policy

## Supported versions

| version | security fixes |
|---|---|
| 0.2.x | yes |
| older than 0.2 | no |

A fix lands in the latest 0.2.x release. Upgrade to that release to get it; see [Upgrading](docs/operations/upgrading.md).

## Reporting a vulnerability

Report it privately through GitHub: on this repository, open **Security**, then **Report a vulnerability**
([direct link](https://github.com/jaredzimmerman/hookspool/security/advisories/new)). Please do not open a public issue,
pull request or discussion about it.

Please include:

- the hookspool version (`hookspool --version`) and the Python version
- which part is affected: the intake, a bobbin's signature check, the dispatcher, a destination or the CLI
- the steps or the request that shows the problem. Use test secrets and made-up payloads, and never send a real secret
  or a real delivery
- what an attacker gains: forged events accepted, events lost, a secret disclosed, or a crash

The maintainer aims to reply within a week. A confirmed issue is fixed in a private advisory, and you are credited in
the advisory and the changelog unless you would rather not be.

## Scope

In scope: a request that gets past signature verification, an acknowledged event that is later lost, a secret
or a payload body showing up in logs or output, a bobbin reaching outside what [ARCHITECTURE 14.3.7](docs/architecture/ARCHITECTURE.md#1437-bobbins-call-core-they-never-re-implement-it-and-never-each-other)
allows, and the CI workflows running untrusted pull request code with write access.

Out of scope: a deployment that publishes the intake without TLS or a reverse proxy, secrets the operator
put in the config file in plain text, and denial of service through traffic volume alone, which the reverse proxy is
there to limit.
