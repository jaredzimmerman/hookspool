# Adding a destination

A destination is where a coalesced batch goes: the thing that does something with it. The dispatcher hands a destination
one batch document, and the destination answers with one outcome. Everything around that (leases, retries, backoff, the
circuit breaker, issues, alerts) is shared, so a destination is small.

## Do you need one?

Three destination types are built in. Most needs are covered by one of them.

| type | name | use it to |
|---|---|---|
| `script` | `script:<name>` | run any program with the batch on standard input. The extension point for almost everything: summarise, filter, call an API, write a file, report new events back |
| `http` | a name you choose, `type = "http"` | POST the batch, signed, to any HTTPS endpoint |
| `hermes` | `hermes:<route>` | wake a Hermes Agent gateway route |

`hermes` and `http` also have an `unwrap` mode for receivers that expect the sender's original payload, one request per
event, instead of the document (see [Unwrapped delivery](#unwrapped-delivery) below).

A **script** is the answer when you can write the work as a program. It gets the document on stdin, exits 0 for success, and
may print `{"enqueue": {...}}` lines to hand new events back to the spool, atomically with the batch being marked
delivered (see [Scripts](#scripts)). Write a destination class only when you need something a subprocess cannot be:
a transport hookspool does not speak, or a connection you want to keep open.

## Scripts

```toml
[destinations."script:digest"]
argv = ["/usr/bin/python3", "/opt/scripts/digest.py", "--batch"]
env_file = "/etc/hookspool/digest.env"
state_dir = "/var/lib/hookspool/scripts/digest"
timeout_s = 300
```

The contract:

- `argv` is run directly (no shell), in its own session, so a timeout kills the whole process group.
- The batch document (below) arrives on standard input.
- The environment is minimal: `PATH`, `HOME` and `LANG` from the dispatcher, whatever `env_file` holds, and
  `HOOKSPOOL_BATCH_ID`, `HOOKSPOOL_DELIVERY_ID` and, when `state_dir` is set, `HOOKSPOOL_STATE_DIR`. The dispatcher's own
  secrets do not reach the script.
- **Exit 0 is success.** Any other exit counts as a failure on this batch and is retried with backoff (it counts toward
  `max_attempts`). A script that cannot start or runs out of time is a transport failure and is retried without counting.
- Each stdout line of the form `{"enqueue": {"source": "...", "key": "...", "payload": {...}}}` becomes a new event, in the
  same transaction that marks the batch delivered. Either the script's report is recorded and the batch is done, or neither.
  The allowed fields are `source`, `key`, `payload` (required) and `action`, `route`, `event_type`, `dedup_id`,
  `quiet_s`, `max_s`, `max_events`, `max_bytes`, `not_before`. An emit with no `action` goes where its route or source says.
  Other output lines are ignored. Output over 1 MiB fails the attempt rather than truncating, since a cut tail would lose emits.
- A script is run once per attempt, and a crash between the script's exit and the mark means it runs again. **Make it
  idempotent**, keyed on `HOOKSPOOL_BATCH_ID` (stable across attempts) or on the events' own ids. A script that keeps
  its own cursor should write the cursor update and its emits to a journal keyed by the batch id before it applies either,
  so a retry replays the journal instead of reading the outside world again; `deploy/scripts/README.md` describes the
  pattern the Gmail and Calendar scripts use.
- Scripts that write anywhere need that path in the service unit's `ReadWritePaths`. `state_dir` is the place.

## The batch document

Every destination, of every type, receives the same JSON document:

```json
{"source": "github", "route": "github-ci", "key": "owner/repo", "batch_id": 123,
 "delivery_id": "hs_...", "attempt": 2, "count": 7, "first_at": 1.7e9, "last_at": 1.7e9,
 "event_type": "workflow_run", "event_types": {"check_suite": 5, "workflow_run": 2},
 "summary": "7 github event(s) for owner/repo (check_suite x5, workflow_run x2)",
 "content_hash": "sha256:...",
 "events": [ {"...": "the original payloads, in arrival order"} ],
 "event_meta": [ {"id": 1, "event_type": "...", "received_at": 1.7e9, "dedup_id": "..."} ]}
```

`events` and `event_meta` are frozen when the batch is sealed and are what `content_hash` covers, so a retry of the same
batch carries the same events in the same order. `delivery_id` and `attempt` change per attempt by design. Events an
operator excluded are omitted. `summary` is a count line, never an interpretation.

HTTP deliveries also carry the [Standard Webhooks](https://www.standardwebhooks.com/) headers `webhook-id`,
`webhook-timestamp` and `webhook-signature` (`v1,<base64 HMAC-SHA256>` over `<id>.<timestamp>.<body>`), plus
`X-Hookspool-Batch` and `X-Hookspool-Attempt`. For `hermes`, `webhook-id` is fresh on every attempt, because Hermes
remembers an id on admission and would answer `duplicate` to a retry of a batch it admitted but did not process. For
`http` and `script` it is the same on every attempt of a batch, so a receiver that keeps ids can drop a redelivery.

### Unwrapped delivery

With `unwrap = true` on a `hermes` or `http` destination, the document is not sent. Each event is one POST, in order, whose
body is the event's stored payload, signed per event, with `webhook-id` `hs_<instance>_<batch>_<event id>` (the same on
every attempt), `X-Hub-Signature-256` over that body, and the optional `event_header` (the event's type) and `dedup_header`
(its dedup id). The batch succeeds only when every event's request did; the first failure stops the attempt with that
request's outcome and the whole batch is retried, so events already accepted are sent again and should be answered
`duplicate`. Configuration and the coalescing advice are in
[CONFIGURATION.md](CONFIGURATION.md#unwrap-one-request-per-event).

## A destination class

Subclass `Destination`, implement `deliver`, and name the class in config.

```python
class Destination:
    type_name: str                  # a label for logs
    idempotency: "per-batch" | "per-attempt" | "per-event"   # how delivery ids are issued; fixed by the type
    unwrap: bool                    # True: one request per event (ctx.payloads holds the stored bodies)
    default_timeout_s: float
    default_max_body_bytes: int | None

    def __init__(self, name: str, spec: dict, config: dict): ...
    def deliver(self, doc: dict, body: bytes, delivery_id: str, ctx: DeliveryContext) -> Outcome: ...
    def check(self) -> list[str]: ...
```

- `name` is the destination's name, `spec` its config table, `config` the whole loaded config. The base class sets
  `self.timeout_s`, `self.max_body_bytes` and `self.max_inflight` from the table.
- `deliver` gets the document, its exact bytes (`body`: send and sign these, not a re-encoding of `doc`), the delivery id and a
  context. It is called from a worker thread.
- `check` returns problems with the table or its secrets, as plain sentences that never contain a secret value.
  `hookspool check` validates the keys of the built-in types itself; it does not yet call a destination's own `check()`,
  so a module destination should also fail safe in `deliver` (a `config` outcome) when its table is wrong.

### Outcomes

`deliver` returns an `Outcome`, built with one of these. The class is what decides whether the failure counts against the
batch and what the circuit breaker does.

| constructor | when | effect |
|---|---|---|
| `Outcome.ok(reason, status=...)` | the receiver took the batch | batch `delivered`; the breaker's success |
| `Outcome.transport(reason)` | could not reach it, timed out, the receiver is down or answered 5xx | retry with backoff, **not counted**, counts toward the breaker. Outages never kill a batch |
| `Outcome.throttle(reason, retry_after_s=...)` | 429, or 503 with `Retry-After` | retry at the receiver's time, not counted, breaker neutral |
| `Outcome.batch(reason)` | the receiver is up but failed on this batch | retry with backoff, **counted** toward `max_attempts`, then dead |
| `Outcome.config(reason)` | a problem the operator must fix: a bad secret, an unknown route, a certificate failure | retry at the backoff cap, not counted, opens a `config` issue; due again at once after a config reload |
| `Outcome.permanent(reason)` | the receiver rejects the document itself (400, 413, 415, 422) | batch goes `dead`, with an issue and an alert. An operator can `split`, `exclude` and `replay` it |

Rules for the text: `reason` is short and goes to logs and alerts, so it never contains a payload, a header value or a
secret. `detail` goes to the batch's `last_error` and is scrubbed to 500 characters, and is never logged. An `Outcome.ok`
may carry `emits` (as scripts do) and a `notice` (an operator-facing note that opens an issue without changing the outcome).

`hookspool.destinations.wire` has the helpers the built-in HTTP destinations use: `post(url, headers, body, budget_s)`
(one POST that cannot outlive its budget), `classify_status(response, now, redirect_permanent=...)` (the status mapping above),
`net_outcome(error, timed_out_reason=...)` and `delivery_headers(...)` (Standard Webhooks signing). Reuse them for an HTTP
transport; they have the timeouts and the status mapping right.

A destination class can offer the unwrap mode too: set `self.unwrap = True` and `self.idempotency = "per-event"` in
`__init__`, and the dispatcher fills `ctx.payloads` with each event's stored payload bytes, aligned with
`doc["event_meta"]`, and applies `max_body_bytes` to each of them. `wire.unwrap_deliver(doc, delivery_id, ctx, send_one)`
runs the loop: it calls `send_one(meta, body, event_delivery_id)` per event, stops at the first outcome that is not `ok`,
and returns it with the event's id and the count delivered before it (`events_delivered`, `failed_event_id`, which the
dispatcher stores on the attempt row).

### The deadline is a hard rule

`ctx.deadline` is the moment the dispatcher's lease on the batch runs out (five seconds before it, in fact). A worker thread
cannot be aborted, so a `deliver` that outlives its deadline holds its worker, its lease and the head of its key until it
returns, and another delivery may overlap it. Every operation inside `deliver` must be bounded:

```python
budget = ctx.timeout(self.timeout_s)      # min(timeout_s, time left); 0 means out of time
```

Pass `budget` to every socket, request or subprocess wait, and return `Outcome.transport("deadline")` when it is spent.
A subprocess must be killed with its process group, not just its parent.

### A worked example

A destination that posts a one-line summary to a chat webhook. The URL is a secret, so it is a reference in the table.

`mychat.py`, anywhere on the dispatcher's Python path:

```python
import json

from hookspool import config as hs_config
from hookspool.destinations import DeliveryContext, Destination, Outcome
from hookspool.destinations.wire import NetError, classify_status, net_outcome, post


class ChatDestination(Destination):
    type_name = "chat"
    idempotency = "per-batch"
    default_timeout_s = 10.0

    def __init__(self, name, spec, config):
        super().__init__(name, spec, config)
        self.url_ref = self.spec.get("url")

    def deliver(self, doc: dict, body: bytes, delivery_id: str, ctx: DeliveryContext) -> Outcome:
        try:
            url = hs_config.resolve_secret(self.url_ref)       # read fresh, so a rotation needs no restart
        except hs_config.SecretError as e:
            return Outcome.config(f"webhook url unusable: {e}")
        text = json.dumps({"text": doc["summary"]}).encode("utf-8")
        budget = ctx.timeout(self.timeout_s)
        try:
            resp = post(url, {"Content-Type": "application/json"}, text, budget)
        except NetError as e:
            return net_outcome(e, timed_out_reason="deadline" if budget < self.timeout_s else "timeout")
        if 200 <= resp.status < 300:
            return Outcome.ok(f"http {resp.status}", status=resp.status)
        return classify_status(resp, ctx.now(), redirect_permanent=True)

    def check(self) -> list[str]:
        try:
            hs_config.resolve_secret(self.url_ref)
        except hs_config.SecretError as e:
            return [f"{self.name}: url: {e}"]
        return []
```

Name it in the config. `module` is `package.module:ClassName`, and any other keys in the table are the class's own (the
core does not validate them, so validate them in `check`):

```toml
[destinations.chat]
module = "mychat:ChatDestination"
url = { file = "/etc/hookspool/secrets/chat_webhook" }
timeout_s = 10
max_body_bytes = 4096

[sources.tickets]
action = "chat"
```

A class that cannot be imported, or whose constructor raises, becomes a stand-in that answers every attempt with a `config`
outcome: batches wait at the head of their key and an issue opens, and nothing is lost or killed. Fixing the module and
reloading drains them.

## Testing a destination

Test it against a stub receiver, not a mock of the thing you are calling, and cover the cases that matter to the dispatcher.
`tests/_httpstub.py` has a scriptable HTTP server (`Stub`), a context builder (`make_ctx`), and a sample document
(`doc_for`). `tests/test_dest_http.py` is the model.

```python
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest

from _httpstub import Stub, doc_for, encode, make_ctx
from hookspool.destinations import build
from mychat import ChatDestination


@pytest.fixture
def stub():
    s = Stub()
    yield s
    s.close()


def dest(make_config, tmp_path, url):
    f = tmp_path / "chat.url"
    f.write_text(url)
    cfg = make_config(destinations={"chat": {"module": "mychat:ChatDestination", "url": {"file": str(f)},
                                              "timeout_s": 2}})
    d = build(cfg, "chat")
    assert isinstance(d, ChatDestination)
    return d


def go(d):
    doc = doc_for()
    return d.deliver(doc, encode(doc), "hs_x_7", make_ctx(wall=True))


def test_a_2xx_is_delivered(make_config, tmp_path, stub):
    o = go(dest(make_config, tmp_path, stub.url + "/in"))
    assert o.kind == "ok"
    assert b"2 github event(s)" in stub.requests[0]["body"]


@pytest.mark.parametrize("status, kind, klass", [(500, "retry", "transport"), (404, "retry", "config"),
                                                 (422, "dead", "permanent"), (429, "retry", "throttle")])
def test_failures_map_to_the_right_class(make_config, tmp_path, stub, status, kind, klass):
    stub.respond({"status": status})
    o = go(dest(make_config, tmp_path, stub.url + "/in"))
    assert (o.kind, o.klass) == (kind, klass)


def test_an_unreachable_receiver_is_a_transport_failure(make_config, tmp_path):
    o = go(dest(make_config, tmp_path, "http://127.0.0.1:9/in"))
    assert (o.kind, o.klass) == ("retry", "transport")


def test_a_receiver_that_never_answers_returns_by_the_deadline(make_config, tmp_path, stub):
    import time
    stub.respond({"status": 200, "delay": 30})
    t0 = time.monotonic()
    o = go(dest(make_config, tmp_path, stub.url + "/in"))
    assert time.monotonic() - t0 < 5
    assert (o.kind, o.klass) == ("retry", "transport")
```

The last test is the one that matters most: a `deliver` that does not return stalls its whole key. The `make_config` fixture
comes from `tests/conftest.py`. Put tests like this in `tests/test_dest_<name>.py`.

## Checklist

- [ ] `deliver` returns by `ctx.deadline` however the receiver behaves, and never raises on purpose
- [ ] every failure is classed: transport for outages, config for what the operator fixes, permanent only for a rejected document
- [ ] no secret, payload or header value in `reason` or in any log line
- [ ] secrets are read from a reference at use time, not cached
- [ ] `max_body_bytes` is set to what the receiver accepts, so `hookspool check` can bound the routes that feed it
- [ ] tests against a stub receiver, including the receiver that hangs
- [ ] `ruff check .` and `python -m pytest -q` pass; a line in `CHANGELOG.md`
