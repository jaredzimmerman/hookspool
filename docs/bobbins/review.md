# Reviewing a bobbin

A bobbin is the plugin for one outside service ([Architecture](../architecture/ARCHITECTURE.md#143-bobbins-service-plugins)). This
page says how a pull request that adds or changes one is judged, so that a contributor knows before opening it what will
be asked, and a maintainer can decide quickly. The short version: **a bobbin-only pull request with a green scorecard and
the three boxes of the template ticked is the expected merge.**

## The one command

```sh
hookspool bobbin check hookspool/bobbins/<name> --base origin/main
```

It runs the conformance kit (`hookspool.testing.bobbinkit`) on the directory and prints a scorecard: every check, its
result, and each problem as `file:line` with a line on how to fix it. It exits 0 only when every required check passes.
`--report md` prints the same scorecard as Markdown, `--report json` as JSON (schema `hookspool.bobbin-check/1`), and
`--strict` makes the advisory check fail the run too. It needs no config and reads no real secret: every secret is a test
value from the bobbin's `captures/kit.toml` (or the default `test-secret-bobbin`).

`bobbin check` runs the bobbin's code: it imports the source, starts the processor and runs the bobbin's tests. The
processor and the tests run in child processes under `python -I` that see only `PATH`, `LANG`, `TMPDIR` and a throwaway
`HOME`, not your tokens or `HOOKSPOOL_*` settings; the source runs in the command's own process. So a maintainer runs
`bobbin check` on a pull request's code only in a throwaway environment (a container or a fresh VM with no
credentials), or leaves it to CI, which runs it with a read-only token and no secrets.

CI runs exactly this command, through `.github/scripts/plugin_ci.py bobbins`, on every bobbin a pull request adds or
changes. There is no check that only CI knows about.

On every pull request CI also runs ruff and the whole test suite on Python 3.11, 3.12 and 3.13, checks that the core
contains no per-source code, scans for secrets and hostnames, and writes the result as a table in the job summary. Run the
same things locally before you push (see [Contributing](../../CONTRIBUTING.md)).

## What green means

| check | it proves |
|---|---|
| `manifest` | `bobbin.toml` is valid, its name is its directory, its `source.module` is not another bobbin's code, and the bobbin covers one service: it reads no other bobbin's signature header and calls no other bobbin's API host |
| `files` | `bobbin.toml`, `README.md`, `SETUP.md`, `AGENTS.md` and at least one capture are there |
| `todos` | no `TODO` placeholder is left (a fresh `hookspool bobbin new` fails only this check) |
| `core-only` | no module re-implements core: no `hmac`, `urllib`, `socket`, `subprocess`, `threading`, `logging`, files, environment, sleeping, `sys.path` or another bobbin (the full list is Architecture 14.3.7). The bobbin's own `tests/` directory is not walked: tests are not modules the runtime loads, and they may open files and run a loopback stub server |
| `docs` | SETUP.md and AGENTS.md name every secret and required setting, AGENTS.md opens with **Needs from the human** and has numbered steps, a step that changes something at the provider is marked `outward`, and the `[bobbins.<instance>]` table in each expands |
| `captures` | at least one accepted and one rejected (bad signature) capture, every `# expect:` holds, and no capture carries a secret-looking value or real-looking personal data (an address outside example.com, a phone number, a public address) |
| `source-kit` | with each accepted capture, the source passes the shared source kit: signed, unsigned, tampered, stale and future timestamps, oversized, chunked, duplicate, the handshakes, quarantine, spool failures, and nothing secret in the spool or the logs |
| `processor` | each capture's payload goes through the real processor runtime with stub HTTP and the network blocked, loopback included (any real socket fails the check; only Unix sockets stay): only declared outputs come back, a second run of the same batch replays the journal, nothing but emits reaches stdout, a failure exits 75 and a permanent one 65 |
| `tests` | the bobbin's own tests (`hookspool/bobbins/<name>/tests/` or `tests/bobbins/test_<name>.py`) pass with the network blocked; loopback is allowed here so a test may run a local stub server |
| `scope` (advisory) | `bobbin-only` when every changed file is inside the bobbin (or its test file); `touches-core` lists the others, `README.md` and `CHANGELOG.md` included (a bobbin's pull request needs neither: the catalogue is generated and the changelog line is the maintainer's). Changed means added, copied, modified or renamed: a pull request that only deletes files runs no bobbin check and gets no label, so review a deletion by hand (the built-in bobbins are re-checked by `tests/test_services_conformance.py` in the main test job) |

## What a maintainer still checks by hand

Green says the bobbin is built the way every bobbin is built. It cannot say that the bobbin is right about the service.
These need a person, and the pull request template asks the contributor for each:

1. **The signature scheme matches the provider's documentation.** The template asks for the link. Read it and compare it
   with what the bobbin declares: the header, the algorithm, what is signed, the timestamp window, and any handshake. A
   captured delivery that verifies is good evidence, but only the documentation says what the provider does on a bad day.
2. **The captures came from a real delivery and were redacted.** The kit catches secret-looking values and real-looking
   addresses; it cannot catch a real customer id or a real name. Skim the bodies.
3. **Someone will maintain it.** The contributor ticks that they can maintain the bobbin, or accept it being removed if it
   breaks and nobody fixes it.
4. **`touches-core` changes are reviewed as core changes.** A pull request that changes anything outside the bobbin (a
   helper in `hookspool/verify.py`, `hookspool/net.py`, `hookspool/providers/`) gets the review a core change gets, and is
   better as its own pull request when it can stand alone.

## The labels and the comment

A separate workflow (`bobbin-label.yml`) reads the scorecards the pull request's run uploaded, checks that the pull request
they name is the one the run was for, and keeps one comment with the scorecards up to date. It never runs the pull
request's code, so it is safe for pull requests from forks.

The label does not come from the scorecards, because the pull request's own code wrote them. The workflow asks GitHub for
the pull request's list of files and applies the `scope` rule to it: `bobbin-only` when every file is inside one
`hookspool/bobbins/<name>/` or is that bobbin's `tests/bobbins/test_<name>.py`, otherwise `touches-core`. The
scorecards' scope is a cross-check. When the two disagree, the label is `touches-core` and the comment says so; read the
whole diff as a core change.

When the bobbin is merged, the maintainer regenerates the catalogue in `docs/bobbins/README.md` with
`hookspool bobbin list --markdown` and adds the `CHANGELOG.md` line.

## Test values: `captures/kit.toml`

Optional; everything in it is a test value. `[secrets]` gives each secret's test value (it must contain `test`),
`[settings]` the instance settings the kit runs with (default: the table in SETUP.md), `body_signed = false` marks a static
token that does not cover the body, `timestamped` and `tolerance_s` describe a timestamp window, and `[[http]]` entries
(`method`, `url` prefix, `status`, `json`) are what `ctx.http` answers during the processor check.
