# Contributing to hookspool

Thank you for looking. The most useful contribution is usually a **new source**: support for one more sender of webhooks.
hookspool is designed so that this is small, safe to review and mechanical to test. Start with that section.

## Adding a sender: the extension model

Every source, built in or contributed, behaves identically once a request is verified. Spooling, dedup, coalescing,
retries, breakers, issues, alerts and metrics are shared code with no per-source variants. A source supplies only what is
about the sender: how it proves itself, what its events coalesce on, a dedup id, a default destination, and sometimes a
handshake. So there is an order of preference:

1. **Config template first.** If the sender signs the raw body with an HMAC, or sends a shared secret in a header or a URL
   parameter, and everything else is a field in the JSON body or a header, then the source is **one TOML table and two
   capture files, with no Python**. This is the preferred route and what most senders need. See
   [docs/ADDING_A_SOURCE.md](docs/ADDING_A_SOURCE.md#a-a-config-template). `examples/sources/github.toml` is the model.
2. **Code plugin when you need it.** A signed timestamp, a JWT, a handshake answer that must be computed, or a payload that
   has to be decoded or built needs a short Python module (`hookspool/sources/<name>.py`, about 30 lines, helpers for every
   common scheme in `hookspool.verify`). See [docs/ADDING_A_SOURCE.md](docs/ADDING_A_SOURCE.md#b-a-code-plugin).
3. **The conformance kit proves it.** `tests/sourcekit.py` runs the same matrix on every source, through the real intake:
   signed, unsigned, tampered, stale and future timestamps, oversized, chunked, duplicate, handshake with a good and with a
   bad signature, plugin exception (quarantine), spool unwritable, low disk, secrets never stored, and no payload ever in a
   log line. A template source gets it for free by adding `examples/sources/<name>.toml` and its captures; a code plugin
   describes itself to the kit in one `Kit(...)` value.
4. **CI enforces it.** On every pull request CI runs ruff and the test suite on Python 3.11, 3.12 and 3.13, runs the kit on
   each source or template the PR touches, checks that the core contains no per-source code, scans for secrets and
   hostnames, and writes the result as a table in the job summary. Run the same things locally before you push (below).

A new source also states its operational facts, which the pull request template asks for: **a link to the sender's
signature documentation**, whether the sender retries a failed delivery, the sweep that keeps it honest, and
`expect_every_s`. Those facts decide whether the source needs a sweep (a sender that never retries does).

Destinations have a similar, smaller path: a `script:` destination covers almost everything, and a destination class is a
few dozen lines. See [docs/ADDING_A_DESTINATION.md](docs/ADDING_A_DESTINATION.md).

## Setting up

You need Python 3.11 or newer.

```sh
git clone https://github.com/jaredzimmerman/hookspool && cd hookspool
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
python -m pytest -q          # the whole suite, about a minute
ruff check .                 # lint
```

The suite is hermetic: a fake clock, temporary spools and state directories, stub HTTP servers on ephemeral ports, test
secrets only, no network. It must stay that way. A test that sleeps for real time should be the exception and should say
why.

Useful runs while you work:

```sh
python -m pytest -q tests/test_sources_template.py -k mysender    # the kit on one template source
python -m pytest -q tests/test_docs.py                             # docs against the code
hookspool source test mysender --request tests/captures/mysender/event.http
python tests/test_sources_template.py --write mysender             # re-sign template captures after editing a body
```

## Rules that keep it trustworthy

These are enforced by tests and reviewed by hand.

- **The core has no per-source code.** Behaviour specific to one sender lives in that sender's plugin or table, never in
  `store.py`, `intake.py`, `dispatch.py`, `config.py` or the destinations. If you find yourself adding an `if source ==`
  anywhere else, the plugin API is missing something; open an issue instead.
- **Standard library only for the core.** `cryptography` and `PyJWT` are allowed for the sources that need them (an
  extra), nothing else. Python 3.11 and newer.
- **No LLM and no network at import time or in tests.**
- **Constant-time comparison only.** A signature, token or digest is compared through `hookspool.verify`, never with `==`.
  A test greps for violations.
- **Verify before everything.** The intake checks the body cap, then the signature, and only then runs anything else of
  yours. A handshake is answered only after the signature verifies, because a handshake answer can be a signing oracle.
  The one exception is a handshake that carries no signature at all and returns a constant (`challenge_unauthenticated`).
- **Never log secrets, headers, query strings or payloads.** Logs carry ids, names, sizes, states, codes and durations.
  The suite plants a marker in every payload and secret and fails if a log line contains it.
- **Our bug must not become the sender's loss.** A plugin that raises after verification quarantines the request instead
  of answering 5xx. Do not catch and swallow errors to avoid that; let them reach the quarantine.
- **Nothing is discarded automatically.** Do not add a path that deletes or drops a batch without an operator.
- **No deployment details in the repository.** No hostnames, addresses, account names, messaging identifiers or real secrets in code,
  tests, captures, docs or examples. Use `example.com`, `example.test`, `127.0.0.1` and obvious placeholders. Real values
  belong in `deploy/local/`, which is git-ignored.
- **Captures and fixtures hold test values only.** A capture is a made-up payload signed with a test secret. Never commit
  a real request, a real signature or a real customer's data, and never derive a fixture from a real secret.

## Pull requests

- Keep a change to one thing. A new source is one pull request: the table or module, its captures and test, and a line in
  `CHANGELOG.md`.
- Write the tests with the change. A bug fix starts with a test that fails.
- Run `python -m pytest -q` and `ruff check .` and make them pass.
- Update the docs the change touches. `tests/test_docs.py` fails when a command, an option or a config key in the code is
  missing from the docs, or the other way round, so the reference pages cannot drift.
- Describe what and why in the pull request, not only what. Fill in the template; for a source, include the link to the
  sender's signature documentation.

Commit messages: a short imperative summary line, then a paragraph on why when it is not obvious.

## Reporting a bug or a vulnerability

Open an issue for a bug, with the command you ran, the output (redact secrets and payloads) and the output of
`hookspool status`. For a suspected vulnerability, such as a way to forge a request that verifies, bypass the body cap, or
read a secret, please use GitHub's private vulnerability reporting on this repository rather than a public issue.

## License

By contributing you agree that your contribution is licensed under the MIT License, the same as the project.
