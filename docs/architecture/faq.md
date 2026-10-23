# FAQ

**Why SQLite and not Redis, Kafka or a hosted queue?** The target is one modest host handling thousands of events a day,
where the hard requirement is "do not lose it", not throughput. SQLite in WAL mode with full sync gives a durable commit with
no extra service to run, back up or secure, and the whole spool is one file you can inspect.

**Is it exactly-once?** No, and nothing that talks to a network can promise it. It is at-least-once with a stable
per-batch id (`webhook-id`) for the receivers that keep ids. Dedup inside the intake (by delivery id or body hash, within
a window) removes most repeats from the sender; receivers still need to be idempotent.

**What if hookspool is down when the sender posts?** The proxy returns 502. Whether the sender tries again is up to the
sender: Google Pub/Sub, Plaid and Zoom do, GitHub does not. That is what the sweep and `expect_every_s` are for, and the
per-source retry facts are in [Sweeps](../operations/sweeps.md).

**What happens when the destination is down for days?** The breaker opens after three transport failures, one probe batch
tries it every 30 seconds to 10 minutes, events keep arriving and sealing in bounded batches, and one issue and one alert
are raised. When the probe succeeds the backlog drains in order, per key. Nothing is dropped and nothing is marked dead
because time passed.

**Do I need Hermes?** No. The Hermes destination exists because the project began as the intake for a Hermes Agent host. A
script or an HTTP destination needs nothing else.

**Can one event go to two places?** A route sends an event to one destination. Send it to a script that delivers it onward,
or emits further events with their own routes.

**How do I choose a coalescing window?** By what a wake-up costs. For something expensive like an agent run, wait for a lull
(`quiet_s`) and cap the wait (`max_s`). For latency-critical events use `immediate`. A burst that goes on forever still
releases at `max_s`, and a batch also seals at `max_events` or `max_bytes`.

**Does it need a reverse proxy?** Yes, for TLS, request buffering and rate limiting. The intake binds loopback only and
answers 411 to a chunked request, which the proxy buffers away. [`deploy/nginx.example.conf`](../../deploy/nginx.example.conf)
is a working configuration.

**How do I add a sender it does not know?** If the sender signs the body with an HMAC or sends a shared secret, it is one
TOML table and two capture files, no Python. Otherwise it is one short module. Either way a shared test kit exercises it
the same way as the built-in sources. See [Writing a bobbin](../bobbins/writing-a-bobbin.md).
