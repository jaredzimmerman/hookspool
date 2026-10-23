# How it compares

hookspool is deliberately narrow. The others are bigger and do more. This is from their public documentation as of October
2026; check theirs for the current picture.

| | hookspool | [Convoy](https://github.com/frain-dev/convoy) | Svix | Hookdeck | Inngest |
|---|---|---|---|---|---|
| **What it is** | an inbound spool in front of one consumer | an open-source webhook gateway for receiving and sending | a platform for sending webhooks to your customers, open-source server and hosted | a hosted service for receiving, queuing and routing inbound webhooks | an event-driven platform for durable functions, with debounce and batching |
| **Runs as** | one package, one SQLite file, two processes | a server with a database and queue | a server with a database, or hosted | hosted only | hosted, or self-hosted |
| **Main direction** | inbound | both | outbound | inbound | inbound events into functions |
| **Coalesces a burst into one delivery, keeping every event** | yes, built in, bounded in events and bytes | not a built-in feature that we know of | not applicable (outbound) | not a built-in feature that we know of | event batching keeps every event; debounce keeps the last |
| **Retries, backoff, circuit breaker** | yes, one breaker per destination, never discards | yes | yes | yes | yes |
| **Replay, dead-letter handling** | CLI: `replay`, `split`, `exclude`, `drop` | UI and API | UI and API | UI and API | UI and API |
| **Dashboard / UI** | none; CLI, `/healthz`, `/metrics` | yes | yes | yes | yes |
| **Fan-out to many endpoints** | no, one destination per route | yes | yes | yes | yes |
| **Operational weight** | lowest: two processes and a file | several components | several components, or none if hosted | none | none if hosted |
| **Choose it when** | one host, one consumer that is costly per call, and the requirement is that nothing is lost | you want a self-hosted gateway with a UI and many endpoints | you are the one sending webhooks to customers | you want a hosted inbound gateway and do not want to run anything | your consumers are functions in a workflow platform |

If you need a dashboard, many consumers per event, a hosted service, or throughput beyond a single machine, use one of the
others. hookspool's niche is the case where a small, self-hosted, inspectable spool with real coalescing is the whole job.
Its deliveries are signed with the Standard Webhooks scheme that Svix and others published, so a receiver written for that
scheme verifies them unchanged.
