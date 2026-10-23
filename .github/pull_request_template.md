## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## Kind of change

- [ ] New source as a config template (`examples/sources/<name>.toml` + `tests/captures/<name>/`)
- [ ] New source as a code plugin (`hookspool/sources/<name>.py` + `tests/test_sources_<name>.py`)
- [ ] Change to an existing source
- [ ] Core, destinations, deploy or docs

## For a new or changed source

**The sender's signature documentation (link):** <!-- required: the page that describes the header, the algorithm and the
timestamp rules you implemented -->

- Signature scheme and header: <!-- e.g. HMAC-SHA256 hex in X-Example-Signature -->
- Does the sender retry on a non-2xx answer? <!-- yes / no / unknown; this sets `sender_retries` -->
- Any handshake (URL validation, a HEAD or ping request)? <!-- describe, or "none" -->

## Checklist

- [ ] The conformance kit passes for this source: `python .github/scripts/plugin_ci.py kit template:<name>` (or `code:<name>`)
- [ ] Captures are signed with a test secret; no real secret, token, hostname, IP or chat id anywhere in the diff
- [ ] Nothing about this source is added to the core (`hookspool/*.py`); the core guard passes
- [ ] `ruff check .` and `python -m pytest -q` pass
- [ ] Docs updated if behaviour or a config key changed
