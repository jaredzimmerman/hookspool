# Contributing to hookspool

Thank you for looking. The most useful contribution is usually a **new bobbin**: support for one more outside service.
hookspool is built so that this is small, safe to review and mechanical to test.

## Adding or changing a bobbin

A bobbin lives in `hookspool/bobbins/<name>/` and is checked by one command, which CI runs too:

```sh
hookspool bobbin new <name> --set scheme=hmac-sha256-hex --set header=X-Signature --set key=id   # a scaffold
hookspool bobbin check hookspool/bobbins/<name> --base origin/main                                # the scorecard
```

* [Writing a bobbin](docs/bobbins/writing-a-bobbin.md) covers the layout, the config-template route (no Python, preferred)
  and the code route, and the capture files.
* [Reviewing a bobbin](docs/bobbins/review.md) says what a green scorecard proves, what CI runs and what a maintainer still
  checks by hand. A pull request that touches only the bobbin, with a green scorecard and the template's three boxes ticked,
  is the expected merge.
* Maintainers: `hookspool bobbin check` runs the bobbin's source, processor and tests, so run it on a pull request's code
  only in a throwaway environment with no credentials, or let CI run it.
* State the operational facts the pull request template asks for: a link to the sender's signature documentation, whether
  the sender retries a failed delivery, the sweep that keeps it honest, and `expect_every_s`.
* Destinations have a smaller path: a `script:` destination covers almost everything. See
  [Adding a destination](docs/configuration/destinations.md).

## Changing the core

Anything outside a bobbin directory is a core change and is reviewed as one. These rules are enforced by tests and reviewed
by hand.

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
secrets only, no network. It must stay that way. A test that sleeps for real time should be the exception and should say why.

```sh
python -m pytest -q tests/test_docs.py        # docs against the code
hookspool source test <instance> --request hookspool/bobbins/<name>/captures/event.http
```

## Pull requests

* Keep a change to one thing. A new bobbin is one pull request: the bobbin directory and its tests. Leave `CHANGELOG.md`
  and the bobbin catalogue alone; the maintainer adds the changelog line and regenerates the catalogue on merge. Any other
  change gets a line in `CHANGELOG.md`.
* Write the tests with the change. A bug fix starts with a test that fails.
* Run `python -m pytest -q` and `ruff check .` and make them pass.
* Update the docs the change touches. `tests/test_docs.py` fails when a command, an option or a config key in the code is
  missing from the docs, or the other way round, so the reference pages cannot drift.
* Describe what and why in the pull request, not only what. Fill in the template.

Commit messages: a short imperative summary line, then a paragraph on why when it is not obvious.

## Reporting a bug or a vulnerability

Open an issue for a bug, with the command you ran, the output (redact secrets and payloads) and the output of
`hookspool status`. For a suspected vulnerability, such as a way to forge a request that verifies, bypass the body cap, or
read a secret, please use GitHub's private vulnerability reporting on this repository rather than a public issue.

## License

By contributing you agree that your contribution is licensed under the MIT License, the same as the project.
