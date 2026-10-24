# zoom

Zoom event subscriptions (meeting and webinar events). Zoom posts each event to the endpoint URL of the
app's Event Subscription. This bobbin verifies the signature, answers Zoom's endpoint URL validation, and
queues each accepted event for the instance's output. There is no processor: the events go straight to the
destination mapped to `"*"`.

## How it works

- **Signature.** Every delivery carries `x-zm-request-timestamp` (unix seconds) and `x-zm-signature`, which is
  `v0=` followed by the hex HMAC-SHA256, keyed with the app's Secret Token, of `v0:<timestamp>:<body>`. The
  window is two-sided, 300 seconds: a stale or future-dated delivery is refused. The check is
  `hookspool.verify.timestamped`.
- **URL validation.** When the endpoint is added, Zoom POSTs `endpoint.url_validation` with a `plainToken`
  and expects `{"plainToken", "encryptedToken"}` back, where `encryptedToken` is the hex HMAC-SHA256 of the
  plain token keyed with the same Secret Token (`hookspool.verify.hmac_hex_sign`). That answer would be a
  signing oracle for anyone allowed to ask, so it is given only after the signature has verified: an unsigned
  or mis-signed validation request is a 401 with no body. A validation never enqueues an event. If an old app
  version does not sign the validation request, add the endpoint with the Secret Token already saved, as
  Zoom's current documentation describes.
- **Key and dedup.** The key is the meeting (or webinar) `uuid`, so one meeting's events coalesce and stay in
  order; an event with no meeting falls back to `payload.object.id`, then the account id, then the event name.
  The dedup id is `<event>:<event_ts>:<uuid or id>`.
- **Span.** `immediate`: a meeting wake-up is not delayed.
- **Event type and attributes.** The event type is the event name (for example `meeting.ended`). Route
  `match` tables can use `event` (the whole name) and `kind` (the part before the first dot, for example
  `meeting`).

## Settings

None.

## Secrets

| name | meaning |
|---|---|
| `webhook_secret` | the Secret Token of the app's Event Subscription |

## Outputs

`"*"`: every accepted event goes to the one mapped destination.

## Limits

- Zoom retries a failed delivery a few times over a few minutes (`sender_retries = true`). An outage of
  hookspool longer than that loses events; Zoom documents no replay of them.
- The Secret Token signs the events and the validation: rotating it at Zoom means saving the new token to the
  secret file before Zoom is told, and Zoom re-runs the validation when the endpoint is saved.
- The raw body is what is signed; a reverse proxy must pass it on unchanged.

## Captures

`captures/event.http` (a signed `meeting.ended`, queued), `captures/stale.http` (signed 301 seconds before it
arrived, unverified), `captures/tampered.http` (body changed after signing, unverified),
`captures/url_validation.http` (signed validation, challenge) and `captures/url_validation_unsigned.http`
(validation without a signature, unverified). They are signed with the test Secret Token in
`captures/kit.toml`.
