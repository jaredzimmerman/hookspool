# Verifying

Run these checks in order. Each one proves something the next depends on, and none needs a real sender.

## The config

```sh
hookspool check
```

It reads the config and prints `ok`, or lists every problem and exits 2. It checks keys, spans, secret references (the files
exist and are not readable by others), routes, destinations and the limits that must nest. The service units run it before they
start. A warning is not a failure; read it anyway.

## A bobbin instance

```sh
hookspool bobbin expand <instance>     # the tables it becomes; secrets stay references
hookspool bobbin show <bobbin>         # what it expects: secrets, settings, outputs
```

## A sender's request, offline

```sh
hookspool source test <instance> --request path/to/capture.http
```

It replays one captured request through verification, the handshake, the ignore rule, the key and the dedup id, and prints what
would happen: `queued`, `ignored`, `challenge` or `unverified`. It never writes to the spool. The capture format is in
[Writing a bobbin](../bobbins/writing-a-bobbin.md#capture-files).

## The pipeline, with a synthetic event

```sh
hookspool migrate                                  # creates the spool
hookspool enqueue --source <name> --key <key> < payload.json   # an event, validated the way the intake does
hookspool status                                   # health, as JSON; the same document /healthz serves
hookspool list                                     # batches and their state
```

`hookspool show <batch> --attempts` shows what was delivered and what happened. The [Quick start](quick-start.md) does this
end to end with real signed requests.

## A bobbin you wrote or changed

```sh
hookspool bobbin check hookspool/bobbins/<name>
```

It prints a scorecard, and every problem as `file:line` with how to fix it. The same command runs in CI.
[Reviewing a bobbin](../bobbins/review.md) says what each check proves.

## A failure, on purpose

Before you rely on an install, run the [gateway-down drill](../operations/drills.md). It shows that the intake keeps answering
while the destination is away, that nothing grows without bound, and that everything is delivered in order afterwards.

## The test suite

From a checkout, `python -m pytest -q` runs the whole suite. It is hermetic: a fake clock, temporary spools, stub servers, no
network. `python -m pytest -q tests/test_docs.py` checks these pages against the code.
