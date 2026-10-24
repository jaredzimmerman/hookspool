---
bobbin: courtlistener.alerts
provider: Free Law Project CourtListener alerts (webhooks)
webhook_docs: https://wiki.free.law/c/courtlistener/help/api/webhooks/about
---

# courtlistener.alerts

CourtListener, the public court-records search run by Free Law Project, POSTs a JSON event to your server when
something you subscribed to changes: a docket gets a new filing, a saved search finds a new match, a RECAP fetch
finishes, a docket alert report is ready, or a pray-and-pay request is granted. This bobbin takes those POSTs,
checks the token in the URL, and queues each event. Events of one type coalesce into one batch.

## Read this first

- **Webhooks are not free for organizations.** CourtListener says it charges reasonable fees to organizations that
  use webhooks, and that it gets in touch with an organization that starts a webhook project to set up an agreement.
  Ask the provider before you build on this bobbin.
- **Alerts are limited by membership.** The alerts you create count against the allowance your membership includes.
  When the allowance is used up, the alerts API refuses a new alert and says why. The provider's pages give no fixed
  number, so check your account's limit.
- **Free alternative: email.** Without an agreement, CourtListener's email alerts still work. A `google.gmail` mail
  rule pack named `courtlistener-alerts` is planned to route those emails into this kind of queue. Until it ships,
  the email alerts go to your mailbox only.

## Events

| `webhook.event_type` | Event | What the payload carries |
|---|---|---|
| 1 | docket alert | `payload.results`: the new docket entries on a subscribed docket |
| 2 | search alert | `payload.results`: the new matches for a saved search |
| 3 | RECAP fetch | the outcome of a RECAP fetch task |
| 4 | old docket alerts report | the docket alerts that have expired |
| 5 | pray-and-pay | a document that was requested and is now available |

The event type is the `webhook.event_type` field in the body. It is both the batch key and the value the outputs see.

## How a delivery is checked

CourtListener does not sign its webhooks. Its documentation says the webhook system has no authentication, and it
recommends a long, random endpoint URL. This bobbin therefore uses a secret token that you choose and put in the
endpoint URL (`?token=...`). A delivery whose token is missing or wrong is refused with 401 and queues nothing.

Each event carries an `Idempotency-Key` header, a UUID for that one event. A redelivery of an event that was already
queued is dropped, so the batch never holds the same event twice.

CourtListener sends from two fixed addresses. SETUP.md shows how to allow only those two addresses at the reverse proxy,
which is optional but recommended.

## Retries

CourtListener retries a delivery that does not get a 2xx answer, up to seven times over about 54 hours, so the
bobbin declares `sender_retries = true` and needs no sweep.

## Secrets

- `secret`: the token in the endpoint URL. Choose a long random value.

## Settings

None. The template is fixed in the manifest.

## Outputs

`*`: every accepted event.

## Captures

- `captures/event.http`: a docket alert (event type 1) with the test token. Expected: queued.
- `captures/wrong_token.http`: the same delivery with another token. Expected: refused.

The captures carry the test token `test-template-secret-PAYLOAD-SENTINEL-7f3a` and a made-up payload.

## Limits

- CourtListener allows one webhook endpoint for each event type and version. To receive several event types, create one
  endpoint per type, all with the same URL.
- The payload shape of each event follows the provider's version number in `webhook.version`. The bobbin reads the
  event type and the dedup key only, so a new version of the payload still queues.
- Redeliveries are recognised by `Idempotency-Key` only. An event that arrives under a new key is a new event.
