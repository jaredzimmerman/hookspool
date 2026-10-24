## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## Kind of change

- [ ] New bobbin (`hookspool/bobbins/<vendor>/<product>/`, made with `hookspool bobbin new <vendor>.<product>`)
- [ ] Change to an existing bobbin
- [ ] Core, destinations, deploy or docs

## New or changed bobbin

Run the conformance kit, the same check CI runs, and paste its first line (the summary):

```sh
hookspool bobbin check hookspool/bobbins/<vendor>/<product> --base origin/main
```

**Summary line:** <!-- e.g. bobbin check acme.invoices: PASS, 10 of 11 required checks pass, 1 not applicable; scope: bobbin-only -->

The provider's developer pages are checked by the scorecard, not here: its `provider` row requires `webhook_docs` and
`signature_docs` in the manifest's `[provider]` table and the same frontmatter in README.md, SETUP.md and AGENTS.md.

Only a person can answer these (see [docs/bobbins/review.md](../docs/bobbins/review.md)):

- [ ] The captures came from a real delivery and were redacted (test secrets, example.com addresses, no real ids or names)
- [ ] I can maintain this bobbin, or accept it being removed if it breaks and nobody fixes it

## The sender

The signature documentation is the manifest's `[provider] signature_docs`; say here what you implemented from it.

- Signature scheme and header: <!-- e.g. HMAC-SHA256 hex in X-Example-Signature -->
- Does the sender retry on a non-2xx answer? <!-- yes / no / unknown; this sets `sender_retries` -->
- Any handshake (URL validation, a HEAD or ping request)? <!-- describe, or "none" -->
- The sweep, and `expect_every_s`: <!-- what reconciles a missed delivery, or why none is needed; 0 or a duration -->

## Checklist

- [ ] The diff stays inside `hookspool/bobbins/<vendor>/<product>/` (and `tests/bobbins/test_<vendor>_<product>.py`, and a
      new vendor's empty `hookspool/bobbins/<vendor>/__init__.py`); the scorecard says `bobbin-only`. Leave the catalogue
      and `CHANGELOG.md` to the maintainer
- [ ] Captures are signed with a test secret; no real secret, token, hostname, IP or chat id anywhere in the diff
- [ ] Nothing about this service is added to the core (`hookspool/*.py`); the core guard passes
- [ ] `ruff check .` and `python -m pytest -q` pass
- [ ] Docs updated if behaviour or a config key changed
