# Writing a bobbin

A **bobbin** is the plugin for one outside service: a directory that holds everything hookspool needs to take that
service's webhooks and turn them into work. A deployment turns it on with one `[bobbins.<instance>]` table in the config
(see [the configuration reference](../configuration/README.md#bobbinsinstance)). This page is for the person writing a new one.

## What goes in a bobbin

```
hookspool/bobbins/<name>/
    bobbin.toml        the manifest: name, secrets, typed settings, outputs, and which source and processor it uses
    source.py          a code source, only when the template cannot express the sender (optional)
    processor.py       work done after the event is committed, such as asking the service what changed (optional)
    captures/*.http    sample requests the checks replay: one accepted, one rejected
    README.md          what it does, its settings, outputs and limits
    SETUP.md           the steps for a person: provider side, secrets, config table, verify, roll back
    AGENTS.md          the same procedure as numbered steps an agent can follow
```

The rules that keep a bobbin small:

- One bobbin per service. `gmail`, `gcal` and `drive` are three bobbins, not one "google" bobbin. Two deployments of
  the same service are two instances in the config, not two bobbins.
- A bobbin never imports another bobbin. Code two bobbins both need moves into hookspool core and both call it.
- A bobbin calls core and never re-implements it. Signatures go through `hookspool.verify`, HTTP and OAuth through
  the context a processor receives, state through `ctx.load_state` and `ctx.save_state`, retries through the dispatcher.
  The full table of what to call is in [Architecture](../architecture/ARCHITECTURE.md#1437-bobbins-call-core-they-never-re-implement-it-and-never-each-other).

## Start from the scaffold

```sh
hookspool bobbin new acme --dir hookspool/bobbins --set scheme=hmac-sha256-hex --set header=X-Acme-Signature --set key=id
```

That copies the [`webhook` bobbin](webhook.md) with your settings frozen into a fixed template, writes an accepted and a
rejected capture signed for the scheme, and writes `README.md`, `SETUP.md` and `AGENTS.md` with `TODO` placeholders.
Add `--code` for a `source.py` instead of the template, and `--processor` for a `processor.py`. Then:

1. Replace the captures with a redacted real delivery from the sender (the capture format is [below](#capture-files)).
2. Fill in the three documents. Every declared secret and required setting must appear in both setup documents, and the
   config table they show must expand without errors.
3. Run `hookspool bobbin check hookspool/bobbins/acme`. It prints a scorecard; fix each `file:line` it lists until every
   required check passes. [Reviewing a bobbin](review.md) says what each check proves and what a maintainer still looks at.
4. Open a pull request that touches only the bobbin directory and its tests. With a green scorecard and the template's
   three boxes ticked, that is the expected merge. Leave the catalogue and `CHANGELOG.md` alone: the maintainer
   regenerates the one and writes the other when merging, and an edit to either makes the pull request `touches-core`.

The rest of this page is about the part that is different for every sender: the source.

## The source of a bobbin

A source is one sender of webhooks: GitHub, a billing system, your own app. Every source behaves identically once a
request is verified. Spooling, dedup, coalescing, retries, breakers, issues, alerts, metrics and the CLI are shared code
with no per-source variants. A source supplies only what is genuinely about the sender:

1. **verify**: how the sender proves a request is from the sender
2. **key**: what its events coalesce on (one batch per key and burst)
3. **dedup id**: how to recognise a redelivered event, if the sender gives one
4. **action**: the default destination
5. **span**: how long to wait for a burst to settle (config, not code)
6. optionally a **handshake** (a verification ping the sender expects an answer to), an **ignore** rule (pings), and an
   **event type** and **attributes** for routing

There are two ways to say this. Try the first one first.

| | when | what you write |
|---|---|---|
| **A. a config template** | the sender signs the raw body with an HMAC, or sends a shared secret in a header or the URL, and everything else is a field of the JSON body or a header | a `[source.template]` table in `bobbin.toml`, two capture files, no Python |
| **B. a code plugin** | the signature covers a timestamp, is a JWT, needs a key fetch or a handshake answer computed from the secret, or the payload has to be decoded or built | `source.py` in the bobbin, about 30 lines of Python |

`hookspool bobbin check` runs the same conformance kit on both, and the kit is what makes a new source safe to merge.

## Before you write anything

Find these out from the sender's own documentation. They decide the table you fill in, and a pull request should state
them (the PR template asks for the link):

- **How requests are signed**, with a link to the sender's page on it. Which header, which algorithm, what is signed
  (the body alone, a timestamp and the body, the body plus the callback URL), how the signature is encoded.
- **Whether the sender retries** a delivery that did not get a 2xx, and for how long. Many senders do not. This decides
  whether hookspool needs a sweep for the source (see below).
- **An id for dedup**, if there is one: a delivery id header or an id in the body.
- **What groups events**: the repository, the project, the mailbox, the meeting. Events with the same key arrive in
  order in one batch.
- **Handshakes**: does the sender ping the URL when you register it, and what does it expect back?
- **How to get a sample request** to turn into a capture, and a way to send a test event.

## A. A config template

A template bobbin has no Python. The sender's rules sit in a fixed `[source.template]` table in `bobbin.toml`, and the
`template` source reads them. This is the GitHub bobbin's whole manifest, `hookspool/bobbins/github/bobbin.toml`:

```toml
name = "github"
version = "1"
summary = "GitHub repository webhook: HMAC-SHA256 over the raw body; pings are acknowledged and not enqueued"

[source]
span = "burst"
sender_retries = false

[source.template]
scheme = "hmac-sha256-hex"
header = "X-Hub-Signature-256"
prefix = "sha256="
key = "repository.full_name"
dedup = "header:X-GitHub-Delivery"
ignore = "header:X-GitHub-Event=ping"
event_type = "header:X-GitHub-Event"
event_header = "X-GitHub-Event"
attrs = { repo = "repository.full_name", event = "header:X-GitHub-Event" }
keep_headers = ["X-GitHub-Event", "X-GitHub-Delivery"]

[secrets.secret]
description = "the webhook secret typed into the repository's webhook settings"

[outputs."*"]
description = "every accepted delivery (pushes, pull requests, and the rest; the event type is in attrs)"
```

A deployment turns it on with an instance that names the secret file and where events go (`[bobbins.github]`, see
[Your first bobbin](../getting-started/first-bobbin.md)); `hookspool bobbin expand github` prints the `[sources.github]`
table that instance becomes.

### Template reference

`scheme` is required and decides which other keys apply (a key a scheme does not use is an error).

| `scheme` | how the sender proves itself | keys |
|---|---|---|
| `hmac-sha256-hex` | `header` holds the hex HMAC-SHA256 of the body, optionally behind `prefix` | `header`, `prefix` |
| `hmac-sha256-base64` | the same, base64 encoded | `header`, `prefix` |
| `hmac-sha1-base64-url` | `header` holds base64 HMAC-SHA1 of the body followed by `callback_url` (Trello) | `header`, `callback_url` |
| `shared-secret-header` | `header` holds the secret itself, optionally behind `prefix` | `header`, `prefix` |
| `url-token` | the query parameter `param` holds the secret. The weakest scheme: the secret sits in the URL, which proxies and senders record. Prefer a signature when the sender offers one | `param` (default `token`) |

All comparisons are constant-time, through `hookspool.verify`.

| key | meaning |
|---|---|
| `scheme` | one of the above |
| `header` | the header that carries the signature or token |
| `prefix` | text before the signature in the header, such as `sha256=` |
| `param` | the query parameter name for `url-token` |
| `callback_url` | the exact URL the sender was given, for `hmac-sha1-base64-url` |
| `head_handshake` | `true` if the sender sends an unsigned `HEAD` when you register the webhook and wants an empty 200. The answer is constant and secret-free |
| `key` | required. What events coalesce on: a selector, or a list of selectors joined with `:` |
| `dedup` | the dedup id: a selector, or `body_sha256` for senders with no id |
| `ignore` | `"<selector>=<value>"` (or a list): requests that are verified, acknowledged and not enqueued, such as pings |
| `event_type` | a label for the event: a selector, or `literal:text` |
| `attrs` | a table of `name = selector`: values a route's `match` can test |
| `sender_retries` | `true` or `false`: whether the sender retries a failed delivery. `check` warns about `false` so the sweep is not forgotten |

A **selector** reads one value from the request:

| selector | reads |
|---|---|
| `a.b.0.c` | a dotted path into the JSON body (object keys and list positions) |
| `header:Name` | a request header |
| `literal:text` | a constant (`key`, `event_type` and `attrs` only) |
| `body_sha256` | the SHA-256 of the raw body (`dedup` only) |

There is deliberately no expression language. If a `key` cannot be read from a request, the request is quarantined (kept,
answered 202, an issue opened) rather than lost, and `hookspool replay <batch>` re-runs it once the table is fixed. If
the sender needs more than the table can say, use a code plugin.

In the manifest, `span`, `sender_retries` and `expect_every_s` go in `[source]` next to the template table, and
`[secrets.secret]` declares the one secret. `keep_headers` (the headers stored with each event) and `event_header` may sit
in the template table as they do for GitHub. An instance can override `span`, the window fields, `expect_every_s`,
`dedup_window_s` and `max_body_bytes` ([the configuration reference](../configuration/README.md#bobbinsinstance)).

### Steps

1. **Scaffold the bobbin**:
   `hookspool bobbin new <name> --dir hookspool/bobbins --set scheme=<scheme> --set header=<header> --set key=<selector>`.
   Give `--set` again for `prefix`, `dedup`, `ignore`, `event_type` or `attrs`, or edit `[source.template]` afterwards. The
   template source checks every value before anything is written. Put the sender's documentation link and its retry
   behaviour in the README.
2. **Put a real delivery in the captures.** The scaffold writes `captures/event.http` (`# expect: queued`) and
   `captures/tampered.http` (`# expect: unverified`), both signed for the scheme with the test secret in
   `captures/kit.toml`. Swap the body of `event.http` for a redacted delivery from the sender: example.com addresses,
   made-up ids, no real names. `tampered.http` stays the same request with the body changed. The format is
   [below](#capture-files). Add the captures that apply:
   - `ping.http`, `# expect: ignored`, if the table has an `ignore` rule
   - `head.http`, `# expect: challenge`, if it has `head_handshake`
3. **Sign the new body.** Compute the signature the way the sender's documentation describes, over the exact body bytes,
   with the test secret from `captures/kit.toml`. For `hmac-sha256-hex` that is
   `openssl dgst -sha256 -hmac '<test secret>' body.json`. Put the result in the signature header and set
   `Content-Length` to the body's size.
4. **Fill in README.md, SETUP.md and AGENTS.md.** Every declared secret and required setting must appear in both setup
   documents, and the `[bobbins.<name>]` table they show must expand.
5. **Run the check**: `hookspool bobbin check hookspool/bobbins/<name> --base origin/main`. It runs the shared
   [conformance kit](#the-conformance-kit) against each capture, so there is no test to write. Fix each `file:line` it
   lists until every required check passes.
6. **Try it offline** the way an operator would: put a `[bobbins.<name>]` instance in a scratch config whose secret file
   holds the test secret, then run `hookspool source test <name> --request hookspool/bobbins/<name>/captures/event.http`.

## B. A code plugin

A code source is `source.py` in the bobbin directory: `hookspool bobbin new <name> --code` writes one, and the
manifest names it with `[source] module = "source"`. It defines these names; only `verify`, `key` and `auth_headers` are
required.

| name | type | meaning |
|---|---|---|
| `auth_headers` | tuple of header names | headers that carry the signature or token. They are never stored and are refused in `keep_headers` |
| `verify(req, secrets) -> bool` | function | is this request from the sender? Return `False` for anything wrong; do not raise |
| `key(payload, req) -> str` | function | the coalesce key. Raise to quarantine a request you cannot key |
| `name` | str | the default source name |
| `secrets` | tuple of names | the secrets the table must provide under `secrets.<name>`; `check` insists on them |
| `keep_headers` | tuple | request headers to store with each event |
| `span` | str | the default span preset; the table's `span` overrides it |
| `action` | str or callable | the default destination, `hermes:<route>`, `script:<name>` or an `http` destination; a callable `(payload, req) -> str` is allowed |
| `expect_every_s` | number | a default liveness expectation |
| `sender_retries` | bool | whether the sender retries a failed delivery. Documentation that `check` and the sweep table use |
| `challenge(req, secrets) -> Response or None` | function | a handshake answer. Runs only after `verify` has passed. Return a `Response` to answer and stop, or `None` to carry on. It never enqueues |
| `challenge_unauthenticated` | bool | `True` only for a handshake that carries no signature at all and gets a constant answer (Trello's `HEAD`). Then `challenge` runs before `verify`, receives no secrets (touching them raises), and may return only a constant |
| `ignore(req) -> str or None` | function | a reason string means "verified, acknowledged, not enqueued" (a ping) |
| `payload(req) -> dict` | function | build the payload when it is not the JSON body (Google Calendar builds it from headers, Gmail decodes it) |
| `dedup_id(payload, req) -> str or None` | function | the dedup id. `hookspool.plugin.dedup.header("X-Delivery")`, `.field("a.b")` and `.body_sha256` cover the common cases |
| `event_type(payload, req) -> str` | function | a label for the event |
| `attrs(payload, req) -> dict` | function | values a route's `match` can test |
| `config_keys` | tuple | extra keys the source's table accepts (Plaid declares `env`) |

The intake runs these in a fixed order and no other: body cap, `verify`, `challenge`, `ignore`, `payload`, then `key`,
`dedup_id`, `event_type` and `attrs`, then route resolution and the write. Anything that raises after `verify` quarantines
the request: the raw body and its headers (minus `auth_headers`) are committed as a dead batch, the sender gets a 202,
and an issue opens. `verify` raising is the one exception and answers 503, so make it return `False` instead.

What a plugin sees:

| | |
|---|---|
| `Request` | `method`, `path`, `query` (a dict), `header(name)` (case-insensitive, `""` when absent), `body` (bytes), `json()` (parsed once; raises `BadPayload`), `route_hint` (the `X-Hookspool-Route` header the proxy sets), `remote_addr`, `now` (the clock to verify against) and `config` (the source's table, without secrets) |
| `Response` | `Response(status, body=b"", headers={})`, and `Response.json(status, obj)` |
| `Secrets` | a mapping that reads each secret when you ask for it: `secrets["signing_secret"]` |
| `BadPayload` | raise it for a body that is not what you expected |

Verification helpers live in `hookspool.verify` (importable as `from hookspool.plugin import verify as v`). They are the
only comparison API plugins get: each ends in a constant-time compare, returns `False` for missing or malformed input and
never raises on what a sender controls. A test greps the code for any other way of comparing a signature.

| helper | for |
|---|---|
| `v.hmac_hex(provided, secret, message, algo="sha256")` | a hex HMAC |
| `v.hmac_b64(provided, secret, message, algo="sha256")` | a base64 HMAC |
| `v.hmac_prefixed(provided, prefix, secret, message)` | `sha256=<hex>` style headers (`encoding="base64"` for base64) |
| `v.timestamped(signature, secret, body, now=req.now, ...)` | a signed timestamp: `t=<ts>,v1=<sig>` in one header, or a separate timestamp header (Zoom's `v0`). Two-sided window, 300 s by default |
| `v.standard_webhooks(msg_id, timestamp, signature, secret, body, now=req.now)` | the Standard Webhooks scheme (`webhook-id`, `webhook-timestamp`, `webhook-signature`) |
| `v.static_token(provided, expected)` | a shared token |
| `v.jwt_es256(token, get_key, body=..., now=req.now)` | an ES256 JWT with a body-hash claim (Plaid); needs the `plaid` extra |

Pass `now=req.now` to timestamped helpers so tests can drive the clock, and keep the window two-sided: a request dated in
the future is as wrong as a stale one.

A sender that publishes its verification keys (a JWKS, or one key per request) is verified through
`hookspool.verify.KeyCache`, not a private fetch. It caches keys for a TTL, refetches once per cooldown on an unknown key id,
and when the sender's key endpoint fails it keeps serving the last keys it had rather than refusing every webhook. That is a
deliberate choice of availability over revocation: a key the sender withdrew during an outage of its key endpoint is still
accepted until the next successful fetch. Pass `usable=` to refuse keys the sender marks expired.

The fetch is built, not written: `v.jwks_fetcher(url)` for a JWKS URL, or `v.key_fetcher(url, method="POST", body=...,
field="key", unknown=...)` for a per-key endpoint that wants credentials (`unknown(status, code)` says which refusals mean
"the sender has no such key"; both go through `hookspool/net.py`, so errors carry no URL, query or body). A cache that every
source of one sender shares is `v.shared_key_cache(name, ...)`; `cache.lookup(kid, req.now, fetch=...)` takes a fetch of its
own for credentials that are read per request. A failed fetch leaves one log line (`<name> key fetch failed: <class>`, with
the HTTP status and the sender's short error code for a `net` error, never a body). The Plaid bobbin is the model.

### A worked example

A sender, `acme`, signs `"<timestamp>.<body>"` with HMAC-SHA256 and sends `t=<timestamp>,v1=<hex>` in `Acme-Signature`.
Events look like `{"id": "evt_1", "type": "invoice.paid", "data": {"customer": "cus_9"}}`. Its documentation says it
retries for three days.

`hookspool/bobbins/acme/source.py`:

```python
"""Acme Billing webhooks. Signature scheme: https://docs.example.com/webhooks/signatures

Acme signs "<t>.<body>" with HMAC-SHA256 and sends "t=<unix seconds>,v1=<hex>" in Acme-Signature.
It retries a failed delivery for three days (sender_retries).
"""
from hookspool.plugin import Request, Secrets, verify as v

name = "acme"
secrets = ("signing_secret",)
auth_headers = ("Acme-Signature",)
keep_headers = ("Acme-Event",)
span = "mail"
sender_retries = True
action = "hermes:acme"


def verify(req: Request, secrets: Secrets) -> bool:
    return v.timestamped(req.header("Acme-Signature"), secrets["signing_secret"], req.body, now=req.now)


def key(payload: dict, req: Request) -> str:
    return payload["data"]["customer"]          # a missing field raises, and the request is quarantined


def dedup_id(payload: dict, req: Request) -> str | None:
    return payload.get("id")


def event_type(payload: dict, req: Request) -> str:
    return payload.get("type", "")
```

`hookspool bobbin check` runs the kit on this source with each capture, using the test secrets in
`captures/kit.toml`. That is enough to merge. To run the whole matrix against a request built in code as well, describe
the source to the kit in a test of the bobbin's own, `hookspool/bobbins/acme/tests/test_source.py`; the `tests` check
runs it with the network blocked. The signing function is written from the sender's documentation with `hmac`
directly, not with `hookspool.verify`, so a bug in a helper cannot hide behind a matching bug in the test:

```python
import hashlib
import hmac
import json

import pytest

from hookspool.bobbins.acme import source as acme
from hookspool.testing import sourcekit
from hookspool.testing.sourcekit import SENTINEL, FakeClock, Kit, request

SECRET = "test-acme-signing-secret"


def sign(raw, secrets, now):
    t = str(int(now))
    mac = hmac.new(secrets["signing_secret"].encode(), t.encode() + b"." + raw.body, hashlib.sha256).hexdigest()
    return raw.set("Acme-Signature", f"t={t},v1={mac}")


BODY = json.dumps({"id": "evt_1", "type": "invoice.paid", "data": {"customer": "cus_9", "note": SENTINEL}}).encode()

KIT = Kit(
    name="acme", module=acme, secrets={"signing_secret": SECRET}, sign=sign, timestamped=True,
    sample=request("POST", "/hooks/acme", {"Content-Type": "application/json", "Acme-Event": "invoice.paid"}, BODY),
)


@pytest.mark.parametrize("case", sourcekit.CASES, ids=sourcekit.CASE_IDS)
def test_kit_matrix(case, tmp_path):
    try:
        sourcekit.run(case, KIT, tmp_path, FakeClock())
    except sourcekit.Skip as e:          # a case that does not apply, such as a handshake acme does not have
        pytest.skip(str(e))
```

`Kit` takes more arguments for the sources that need them: `handshake` (an unsigned handshake request that `challenge`
answers after verification), `unauth_handshake` (for a `challenge_unauthenticated` source), `timestamped=True` with
`tolerance_s` for a signed time window, `body_signed=False` for a static token that does not cover the body (the
tampered-body case then does not apply), `break_stage` (the plugin function the quarantine case makes raise) and
`source_cfg` (extra keys for the source's table). The `zoom` bobbin (a timestamp and a handshake), `gcal` (a token and a
payload built from headers) and `plaid` (a JWT) are the fuller models; their kit tests are `tests/test_sources_zoom.py`,
`tests/test_sources_gcal.py` and `tests/test_sources_plaid.py`.

A code source's captures sit in the bobbin's `captures/`, like a template's. Sign each one at the time the capture says
(`# now:`), so a timestamped scheme verifies when the kit replays it.

## The conformance kit

`hookspool.testing.sourcekit` drives each source through the real intake with a fake clock, a temporary spool and test
secrets; `hookspool bobbin check` runs it as its `source-kit` check, once per accepted capture. Every case below must
pass; one that does not apply to your source (no handshake, no timestamp) is skipped.

| case | what it proves |
|---|---|
| signed | a correctly signed request is queued and answered 200 |
| unsigned | a request with no signature is 401 with an empty body and nothing is stored |
| tampered | a signed request with a changed body is 401 |
| stale, future | for a timestamped scheme, a request dated outside the window on either side is 401 |
| oversized | a body over the cap is 413 before any plugin code runs |
| chunked, no length | a chunked or length-less request is 411 |
| duplicate | the same event twice is queued once and answered `duplicate` |
| handshake | a handshake with a good signature is answered, and never enqueued |
| handshake with a bad signature | a handshake with a bad signature is 401, never answered (a handshake answer can be a signing oracle) |
| unauthenticated handshake is constant | a signature-less handshake gives the same answer whatever is sent, and never touches a secret |
| plugin exception | a plugin that raises after `verify` quarantines the request (202) instead of failing it |
| spool unwritable | if the spool cannot take the write the answer is 503, never a 2xx |
| low disk | below `limits.min_free_bytes` the answer is 503 with `Retry-After` |
| secrets not stored | no secret value appears anywhere in the database, quarantine rows included |
| logs | no log line carries a payload, secret or header value (the sentinel check) |

## Capture files

A capture is a plain-text HTTP request, the input of `hookspool source test` and of the kit. Metadata lines come first,
then the request line, the headers, a blank line and the body:

```
# capture: invoice paid, signed with the test secret
# now: 1800000000
# expect: queued
POST /hooks/acme HTTP/1.1
Content-Type: application/json
Acme-Event: invoice.paid
Acme-Signature: t=1800000000,v1=...
Content-Length: 87

{"id":"evt_1","type":"invoice.paid","data":{"customer":"cus_9","note":"PAYLOAD-SENTINEL-7f3a"}}
```

| line | meaning |
|---|---|
| `# now: T` | the unix time the request was signed at, which `source test` verifies at |
| `# expect: R` | the result the kit requires: `queued`, `ignored`, `challenge` or `unverified` |
| `# tamper_of: FILE` | this capture is FILE's request with a changed body, still carrying FILE's signature |
| `Content-Length` | when present, the body is exactly that many bytes; when absent, the rest of the file minus one trailing newline |

Captures hold test secrets and made-up payloads only. Never commit a real secret, a real signature of real data, or a
real customer's payload.

## Wiring and the sweep

These are the bobbin's SETUP.md steps, and a deployment follows them:

1. Add a `[bobbins.<instance>]` table to the config, with `secrets.*` pointing at files (see
   [the configuration reference](../configuration/README.md#bobbinsinstance)).
2. Add an nginx `location /hooks/<instance>` with its own `limit_req`; the pattern is in `deploy/nginx.example.conf`.
3. Run `hookspool check`, then `hookspool reload`.
4. Register the URL with the sender and send its test event. `hookspool list --source <instance>` shows it.

A webhook replaces a poll but never completely: channels expire, subscriptions get disabled after failures, and some
senders never retry. So every source states, in its pull request and in the sweep table of [Sweeps](../operations/sweeps.md):

- **the sweep**: a cheap, idempotent job that reconciles what the push might have missed, run once or twice a day, or the
  reason none is needed. It should go through hookspool (`hookspool enqueue`, or a script's emitted events) so it is
  coalesced and audited like everything else
- **`expect_every_s`**: how long silence can last before a `source_silent` issue opens, or `0`
- **whether the sender retries**, and for how long. When it does not, the sweep must redeliver failures (GitHub's does)
- **the paths its scripts write**, for the service unit's `ReadWritePaths`, if it has a `script:` destination

## Checklist

- [ ] the sender's signature documentation is linked
- [ ] `hookspool/bobbins/<name>/` made with `hookspool bobbin new`: a `[source.template]` table or a `source.py`
- [ ] captures in `hookspool/bobbins/<name>/captures/`: a signed event, a tampered one, and a ping or handshake if the
      sender has one
- [ ] `hookspool bobbin check hookspool/bobbins/<name> --base origin/main` passes every required check and says
      `bobbin-only`
- [ ] the full suite passes (`python -m pytest -q`) and `ruff check .` is clean
- [ ] no hostname, address, account name or real secret anywhere in the change
- [ ] `sender_retries`, the sweep and `expect_every_s` are stated
