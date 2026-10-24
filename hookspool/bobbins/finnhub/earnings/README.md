---
bobbin: finnhub.earnings
provider: Finnhub Webhooks
webhook_docs: https://finnhub.io/docs/api/webhook
---

# finnhub.earnings

Receives Finnhub's earnings announcement webhook. You create one webhook per ticker through Finnhub's REST API
(`{"event": "earnings", "symbol": "AAPL"}`), and Finnhub POSTs a JSON body to the instance URL when an announcement
for that ticker is published. Each accepted delivery is queued with the ticker as its key and is delivered at once
(span `immediate`: no quiet period, no batching window) to the instance's output.

## What the provider documents, and what it does not

The Finnhub webhook page (`https://finnhub.io/docs/api/webhook`) is thin. When this bobbin was written it did not
describe the signature header, the delivery body, the retry behaviour or the webhook limits per plan, and the
REST API reference (`https://finnhub.io/docs/api`) does not list the webhook endpoints either. What this bobbin
assumes, and the one thing to check first:

- **Verification.** The sender puts a static secret in the `X-Finnhub-Secret` header. The bobbin compares the header
  with the `secret` value, in constant time, and nothing else. The header name comes from third-party integrations
  and from Finnhub's announcement of the feature (<https://github.com/finnhubio/Finnhub-API/issues/94>), not from the
  Finnhub page itself.
- **Payload shape.** Not documented on the pages above. The key is read from `data.0.symbol`, following the envelope
  Finnhub's trade webhook uses (`{"data": [...], "type": "..."}`). Confirm this against the first real earnings
  delivery before relying on it. If the ticker sits somewhere else, the key path in `bobbin.toml` is the one line
  to change.
- **Retries.** Not documented. The bobbin does not assume the sender retries (`sender_retries` is left unset).
- **Limits.** Finnhub does not document how many webhooks a plan allows. Check the account before creating one
  webhook per ticker for a long watch list.

## How it works

1. The intake checks the `X-Finnhub-Secret` header against the `secret` file. A wrong or missing value is refused
   (the request is not queued).
2. The body is hashed with SHA-256 and that hash is the delivery id, so an identical redelivery is dropped.
3. The event is keyed by `data.0.symbol` and sent to the instance output at once.

## Secrets

- `secret`: the value Finnhub sends in `X-Finnhub-Secret`. Finnhub's pages do not say where it is shown; third-party
  guides point to the Webhooks section of the Finnhub dashboard. It is a file reference in the config, never a value.
- The Finnhub API key is not a bobbin secret. The setup steps read it from a file for the one-time webhook creation
  calls and never print it.

## Settings

None. A fixed template: the scheme, header, key and dedup are the bobbin's own.

## Outputs

- `"*"`: every accepted earnings delivery. The bobbin does not split outputs by event type, because Finnhub's
  payload type field has not been confirmed.

## Captures

- `captures/event.http`: an earnings-shaped delivery signed with the test secret. Expected: queued.
- `captures/bad_signature.http`: the same shape with the wrong secret header. Expected: refused (unverified).

The captures use made-up values and the test secret `test-template-secret-PAYLOAD-SENTINEL-7f3a`. They are not real deliveries, so the
first real delivery should replace one of them before this bobbin is treated as verified.

## Limits

- One webhook per ticker, created at Finnhub. Adding a ticker means one more `webhook/add` call.
- No processor: the bobbin does not call the Finnhub API, fetch the announcement or enrich it.
- No retry or sweep is configured. A delivery Finnhub does not repeat is not recovered by this bobbin.

See [SETUP.md](SETUP.md) for the provider steps and [AGENTS.md](AGENTS.md) for the same procedure as numbered steps.
