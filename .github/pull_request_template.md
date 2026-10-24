## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## Kind of change

- [ ] New bobbin (`hookspool/bobbins/<name>/`, made with `hookspool bobbin new`)
- [ ] Change to an existing bobbin
- [ ] Core, destinations, deploy or docs

## New or changed bobbin

Run the conformance kit, the same check CI runs, and paste its first line (the summary):

```sh
hookspool bobbin check hookspool/bobbins/<name> --base origin/main
```

**Summary line:** <!-- e.g. bobbin check acme: PASS, 8 of 9 required checks pass, 1 not applicable; scope: bobbin-only -->

Only a person can answer these (see [docs/bobbins/review.md](../docs/bobbins/review.md)):

- [ ] The provider's documentation for the signature scheme is linked here, and the bobbin implements what it says: <!-- link -->
- [ ] The captures came from a real delivery and were redacted (test secrets, example.com addresses, no real ids or names)
- [ ] I can maintain this bobbin, or accept it being removed if it breaks and nobody fixes it

## The sender

**The sender's signature documentation (link):** <!-- required: the page that describes the header, the algorithm and the
timestamp rules you implemented -->

- Signature scheme and header: <!-- e.g. HMAC-SHA256 hex in X-Example-Signature -->
- Does the sender retry on a non-2xx answer? <!-- yes / no / unknown; this sets `sender_retries` -->
- Any handshake (URL validation, a HEAD or ping request)? <!-- describe, or "none" -->
- The sweep, and `expect_every_s`: <!-- what reconciles a missed delivery, or why none is needed; 0 or a duration -->

## Checklist

- [ ] The diff stays inside `hookspool/bobbins/<name>/` (and `tests/bobbins/test_<name>.py`); the scorecard says `bobbin-only`.
      Leave the catalogue and `CHANGELOG.md` to the maintainer
- [ ] Captures are signed with a test secret; no real secret, token, hostname, IP or chat id anywhere in the diff
- [ ] Nothing about this service is added to the core (`hookspool/*.py`); the core guard passes
- [ ] `ruff check .` and `python -m pytest -q` pass
- [ ] Docs updated if behaviour or a config key changed
