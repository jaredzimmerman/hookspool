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
6. **Whether the sender retries** a failed delivery: `sender_retries`.

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
(`hookspool bobbin show webhook`). To route by event type, add `outputs` entries named after the event types.

## Reverse proxy

Pass `/hooks/acme-events` through with its path unchanged. For `url-token`, keep the query string.

## Apply and check

1. `hookspool bobbin expand acme-events` prints the expanded source with the secret as a reference only.
2. `hookspool check`, then `hookspool reload`. A refused setting is reported with the template's own message
   (for example `header: required by scheme hmac-sha256-hex`); fix the table and run both again.

## Register the webhook

3. At the sender, create the webhook with the URL `https://<HOOKS_HOST>/hooks/acme-events` and the secret from
   above.

## Verify

1. Trigger an event at the sender. `hookspool list --source acme-events` shows a queued event.
2. To test without the sender, save one delivery as a capture (see `captures/event.http` for the format),
   then `hookspool source test acme-events --request <capture>` shows how it is read.
3. A delivery with a wrong signature or token is refused and shows no event.

## Roll back

Delete the webhook at the sender, remove the `[bobbins.acme-events]` table, then run `hookspool check` and
`hookspool reload`.
