# Your first bobbin

A bobbin is the plugin for one outside service. This page turns on the built-in `github` bobbin and tries it offline, with
the sample requests that ship with it, so nothing needs to be exposed to the internet. It assumes you have done the
[Quick start](quick-start.md), or at least installed hookspool and set `HOOKSPOOL_HOME` to an empty directory.

## 1. See what is available

```sh
hookspool bobbin list              # every bobbin found, with its one-line summary
hookspool bobbin show github       # its secrets, settings and outputs
```

## 2. Write an instance

An instance is one table in `config.toml`. It names the bobbin, points at a secret file, and says where the events go. Here
the destination is the demo script from the quick start; for a real setup it would be `hermes:<route>` or an HTTP
destination.

```sh
mkdir -p "$HOOKSPOOL_HOME/secrets"
printf 'test-template-secret-PAYLOAD-SENTINEL-7f3a' > "$HOOKSPOOL_HOME/secrets/github-ci"
chmod 600 "$HOOKSPOOL_HOME/secrets/github-ci"
```

That value is the test secret the bobbin's sample requests are signed with. A real webhook gets a long random secret that
you generate and also type into GitHub.

```toml
[bobbins.github-ci]
bobbin = "github"
secrets.secret = { file = "/path/to/demo/secrets/github-ci" }
outputs = { "*" = "script:show_batch" }

[destinations."script:show_batch"]
argv = ["/path/to/demo/show_batch.sh"]
state_dir = "/path/to/demo/out"
```

Use your real paths in place of `/path/to/demo`.

## 3. Look at what it expands to

```sh
hookspool bobbin expand github-ci
```

This prints the ordinary tables the instance becomes: a source with GitHub's signature rules, your secret as a reference
(never the value) and the destination you chose. `hookspool check` prints `ok`, with a warning that GitHub does not retry a
failed delivery, which is true and is what the [sweep](../operations/sweeps.md) is for.

## 4. Replay the sample requests

The bobbin ships requests captured from a sample delivery. Replay them against your instance without starting any server and
without touching the spool:

```sh
hookspool source test github-ci --request hookspool/bobbins/github/captures/event.http      # result: queued
hookspool source test github-ci --request hookspool/bobbins/github/captures/ping.http       # result: ignored
hookspool source test github-ci --request hookspool/bobbins/github/captures/tampered.http   # result: unverified
```

The push is queued, GitHub's ping is acknowledged and not queued, and the one with a changed body is refused with a 401.

## 5. Go live

The bobbin's own `SETUP.md` has the rest, in order: create the webhook in the repository, the reverse proxy line, how to verify
a real delivery, the sweep, and how to roll back. Every bobbin has the same files, listed in the
[catalogue](../bobbins/README.md). To cover a service that has no bobbin, see [The webhook bobbin](../bobbins/webhook.md).
