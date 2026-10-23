# Set up a service through webhook

For a web service that signs or tokens its webhook deliveries and has no bobbin of its own. You read the
sender's webhook documentation into a handful of settings. Pick an instance name for the service (below,
`acme-events`); it becomes the URL `/hooks/acme-events`.

## What to find in the sender's documentation

1. **How a delivery proves itself.** Which header carries the signature or token (or which query parameter),
   what text sits in front of it, and what is signed. Choose the `scheme` that matches: the hex or base64 HMAC
   of the raw body, the HMAC that also covers the callback URL, the secret itself in a header, or a token in the
   URL.
2. **Which field identifies the object.** The payload path (or header) that events about one object share:
   that is the `key`.
3. **Which header or field carries a delivery id**, if any: that is `dedup`.
4. **Which deliveries are noise** (a ping on creation, for example): each is an `ignore` entry.
5. **Where the event type is** (a payload path or header), and any value worth routing on (`attrs`).
6. **Whether the sender retries** a failed delivery: `sender_retries` (see "Sweep" below).

## Secrets to save

- The signing secret or token the sender shows for the webhook: write it to
  `/etc/hookspool/secrets/acme-events`, mode 0600, owned by the hookspool user. Nothing else is in that file.

## Config table

```toml
[bobbins.acme-events]
bobbin = "webhook"
settings = { scheme = "hmac-sha256-hex", header = "X-Signature", prefix = "sha256=", key = ["id"], dedup = "header:X-Delivery-Id", event_type = "type", attrs = { event_type = "type" } }
secrets.secret = { file = "/etc/hookspool/secrets/acme-events" }
outputs = { "*" = "hermes:acme-events" }
```

Replace the settings with what you found. `scheme` and `key` are required; the rest depends on the scheme
(`hookspool bobbin show webhook`). To route by event type, add `outputs` entries named after the event types
**and** keep `event_type` and `attrs = { event_type = "<the same selector>" }` in the settings (the routes read
the event's attributes); `hookspool check` reports an error when `attrs.event_type` is missing.

## Reverse proxy

Pass `/hooks/acme-events` through with its path unchanged. For `url-token`, keep the query string.

## Apply and check

1. `hookspool bobbin expand acme-events` prints the expanded source with the secret as a reference only.
2. `hookspool check`, then `hookspool reload`. A refused setting is reported with the template's own message
   (for example `header: required by scheme hmac-sha256-hex`); fix the table and run both again.
3. Test with a captured delivery before registering anything: save one delivery as a capture (see
   `captures/event.http` for the format; sign it with the secret by hand or use a delivery the sender recorded),
   then `hookspool source test acme-events --request <capture>` shows how it is read: `result` is `queued`
   with the key, event type and attributes you expect. `unverified` means the scheme, header, prefix or secret
   differs from the sender's.

## Register the webhook

4. At the sender, create the webhook with the URL `https://<HOOKS_HOST>/hooks/acme-events` and the secret from
   above.

## Verify

1. Trigger an event at the sender. `hookspool list --source acme-events` shows a batch for it.
2. A delivery with a wrong signature or token is refused and shows no batch.

## Sweep

`hookspool check` warns `the sender does not retry failed deliveries; its sweep must redeliver` for every
instance whose `sender_retries` is false (the default). If the sender does retry, set `sender_retries = true`
and the warning goes. If it does not, a delivery refused while hookspool was down is lost unless something
asks the sender again. Install a small periodic job (daily is enough) that lists the sender's recent failed
deliveries and redelivers them, or that reconciles the sender's own state; redeliveries are harmless because
`dedup` recognises a delivery already queued. Read the sender's documentation for a "recent deliveries" or
"redeliver" call; if it has none, the job reads the object list instead and enqueues what is missing with
`hookspool enqueue --source acme-events ...`. Set `expect_every_s` on the instance when the sender normally
sends something within a known period, so a silent source opens an issue.

## Roll back

Delete the webhook at the sender, remove the `[bobbins.acme-events]` table, then run `hookspool check` and
`hookspool reload`.
