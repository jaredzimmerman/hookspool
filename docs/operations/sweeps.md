# Sweeps

A webhook replaces a poll but never fully ([Architecture](../architecture/ARCHITECTURE.md), section 13). This page says
which senders retry and what keeps the rest honest. Every migrated job keeps a sweep
that is cheap (at most two runs a day), idempotent with the push path, and replaced by `expect_every_s` where the
push covers it completely. If a source's `expect_every_s` passes with no event, a `source_silent` issue opens: that
is the signal that the push path broke and the sweep is carrying the load.

| source | old job (generic) | sweep | cadence | `expect_every_s` | sender retries a failed delivery? | script state paths |
|---|---|---|---|---|---|---|
| GitHub | none (the gateway took the POST directly, no buffer) | `sweeps/github_redeliver.py` per hook: lists the hook's deliveries, redelivers those that never got a 2xx | twice a day | `0` (or a few days on a busy repo) | **No.** Redelivery is manual (UI or `POST …/deliveries/{id}/attempts`). A 503, 429 or timeout is a loss until the sweep runs (a 413 is a loss the sweep cannot fix; see the first limit below) | none |
| Plaid | the daily transaction sync | keep the daily sync as is (it reconciles by cursor); the webhook only tells the agent sooner | once a day | `0`: item activity is irregular | Yes, with backoff for hours | none (the sync's own paths stay as they are) |
| Gmail (Pub/Sub) | the mail-arrival poll | renew the mailbox `users.watch` before its 7-day expiry (a cheap `users.watch` call) and let the `google.gmail` bobbin reconcile from its stored history id | once a day | `345600` (4 days) | Yes, for the subscription's retention (days) | `<home>/bobbins/gmail` |
| Calendar | the calendar poll plus an in-memory debounce | renew the watch channel before it expires (channels last at most a week; Google does not warn); the `google.calendar` bobbin reconciles from its sync token | once a day | `172800` (2 days) | Yes, with backoff for a limited time | `<home>/bobbins/gcal` |
| Drive | none (new) | renew the change-feed watch channel before it expires (channels last at most a week; Google does not warn); the `google.drive` bobbin reconciles from its stored page token | once a day | `604800` (1 week) | Yes, with backoff for a limited time | `<home>/bobbins/drive` |
| Meet | none (new) | renew the Workspace Events subscription before its 7-day expiry (`PATCH …/v1/subscriptions/<id>?updateMask=ttl` with `{"ttl": "0s"}`; lifecycle events also arrive as `subscription-changed` items); an expired subscription is deleted and must be created again | once a day | `604800` (1 week) | Yes, for the subscription's retention (days) | `<home>/bobbins/meet` |
| Trello | the board poll | list the app's webhooks; re-create any missing or `active: false` (Trello disables a webhook that keeps failing) | once a day | `0` (boards are quiet) | A few times, then may disable the webhook | none |
| Zoom | the meeting poll | none for events; check the subscription still exists | on demand | `0` | Yes, a few times over minutes | none |
| anything else | – | state it when the source is added ([Writing a bobbin](../bobbins/writing-a-bobbin.md#wiring-and-the-sweep)) | – | – | assume **no** until shown | – |

Rules that make this honest:

* A sweep that finds work the push missed should **go through hookspool** (`hookspool enqueue`, or a `script:` emit),
  so it gets the same coalescing, dedup and audit trail.
* A sweep must not re-announce what the push already delivered; use the same `dedup_id` the source would.
* Every script `state_dir` is in the dispatch unit's `ReadWritePaths`. If a sweep runs under cron as a different
  user, give that user the same paths.
* The cron line for the GitHub sweep, per hook:
  `17 3,15 * * * /usr/bin/python3 /opt/hookspool/sweeps/github_redeliver.py --repo OWNER/REPO --hook-id 123456 >> /var/log/hookspool-sweep.log 2>&1`
  (with `GH_TOKEN` in the environment; fine-grained token, repository webhooks read and write, nothing else).

### Known limits, stated plainly

| limit | what happens | what to do |
|---|---|---|
| **GitHub payload over the source `max_body_bytes`** (768 KiB by default: the route's `max_bytes`, 786432, caps it; large pushes, long PR bodies, many commits) | The request gets **413** before any plugin code runs. `[intake] max_body_bytes` and nginx's `client_max_body_size` are 1 MiB, so a body between 768 KiB and 1 MiB passes nginx and is refused by the intake; above 1 MiB nginx refuses it. GitHub does not retry it, so it is not delivered. The redelivery sweep does not redeliver it (it would get the same 413) and counts it in its summary line. | Raise the limit end to end, in this order: the destination (`hermes.max_body_bytes`, and the gateway's own limit), the route `max_bytes` (at most 0.75 × the destination), the source `max_body_bytes` (at most the route's), `[intake] max_body_bytes`, nginx `client_max_body_size`. `hookspool check` enforces the order. GitHub itself caps payloads at 25 MB. Deliveries the sweep skipped for a 413 stay undelivered after you raise the limit: redeliver those once by hand (the hook's Recent Deliveries page, or `POST …/deliveries/{id}/attempts`). Watch `hookspool_requests_rejected_total{reason="too_large"}`. |
| **Plaid key-fetch limit** | A `kid` that is not cached (a key rotation, or the first key after an intake restart) shares **one key fetch a minute** per Plaid environment. A client sending random-`kid` requests about once a second can hold such a key at 401 until it stops, or until Plaid stops retrying. Known keys are cached for 24 h and a known key is still served if a refresh is blocked, so this concerns only a key not seen yet. | The nginx `limit_req` zone for `/hooks/plaid` slows an abusive address (and Plaid retries over hours, so a genuine rotation gets in once the slot frees). A small token bucket for fetches is a later option and is not built. Watch `hookspool_requests_rejected_total{source="plaid",reason="unverified"}` for a spike. |
| **Gmail `messageId` is per push** | If the intake is killed after commit and before answering, Pub/Sub redelivers with a new id and the batch holds two events. | Harmless: the `google.gmail` processor reads Gmail history once per batch. |
| **Pub/Sub token in the URL** | The weakest verifier of the set. It is in the subscription config, and anything that records the request line records it. nginx does this in more places than the access log: its error log writes the request line and the upstream URI (query string included) on every `limit_req` rejection and every 502 while the intake restarts. | The example keeps it out of nginx's logs: the access log format writes `$uri` only, the port-80 server has `access_log off`, and `location = /hooks/gmail` (and `/hooks/meet`) has its own `error_log … crit` with `limit_req_log_level warn`, so those lines are not written (the access log still shows a 429 or 502 for the path). **What remains:** the subscription config in Google Cloud; any proxy, CDN or load balancer in front of nginx and its logs; the `crit`/`emerg` lines nginx writes for that location (rare: out of memory, for example); and nginx's `info` or `debug` error levels if you set them on the main `error_log`, so do not (debug logging of a webhook host exposes every URL). If a log is ever found to hold a token, rotate it: write a new push token to the secret file, then update the subscription's endpoint (only one token is accepted at a time, so pushes in between get 401 and Pub/Sub retries them). OIDC verification of the push is a noted later hardening (ARCHITECTURE section 10). |
| **Loopback bind** | The intake is reachable only through the proxy. | Do not bind it to a public address. |
