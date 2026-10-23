# Install

## What you need

- Python 3.11 or newer. The core uses only the standard library.
- A shell. Linux and macOS are what the tests run on.
- For the Plaid bobbin only, two extra packages: `cryptography` and `PyJWT`.
- To receive requests from the internet, a reverse proxy in front of hookspool. The intake listens on loopback only.
  You do not need one to try it locally.

## From a checkout

```sh
git clone https://github.com/jaredzimmerman/hookspool && cd hookspool
python3 -m venv .venv && . .venv/bin/activate
pip install .                  # or: pip install '.[plaid]' for the Plaid bobbin
hookspool --help
```

`pipx install .` works as well, and puts the `hookspool` command on your path. For development, install the test tools
with `pip install -e '.[dev]'` (see [Contributing](../../CONTRIBUTING.md)).

## Where it keeps things

Everything hookspool writes lives in one state directory: the config file, the spool (one SQLite file) and a lock file.
The default is `/var/lib/hookspool`. For a local trial, point it somewhere else:

```sh
export HOOKSPOOL_HOME="$PWD/demo"
```

The order of precedence is the `--home` option, then `HOOKSPOOL_HOME`, then `[spool] home` in the config, then the default.
Secrets are never stored in the config; the config holds references to files or environment variables.

## Two processes

hookspool is two small programs that share the spool file:

- `hookspool intake` takes requests, checks them and writes them to disk before answering.
- `hookspool dispatch` delivers what the intake stored.

You start both. `hookspool run` runs the two in one process, which suits a trial or a very small host; production runs them as separate services.

## Next

[Quick start](quick-start.md) runs a complete local demo. To install on a server, with systemd units and a reverse proxy, go to
[Running hookspool](../operations/running.md).
