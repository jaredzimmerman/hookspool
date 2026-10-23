# The webhook bobbin

Most web services send the same kind of request: a JSON body, signed with a shared secret or sent with a token. The
`webhook` bobbin covers all of them without any code. You describe the sender in the instance's `settings`, copying the
facts from the sender's own documentation, and hookspool does the rest.

## An instance

```toml
[bobbins.invoices]
bobbin = "webhook"
secrets.secret = { file = "/etc/hookspool/secrets/invoices" }
settings = { scheme = "hmac-sha256-hex", header = "X-Invoices-Signature", prefix = "sha256=", key = ["customer.id"], dedup = "id", sender_retries = true }
outputs = { "*" = "hermes:invoices" }
```

The URL to give the sender is `/hooks/invoices`, and the secret is the one the sender shows you for the webhook.

## What the settings say

| setting | answers |
|---|---|
| `scheme` | how the sender proves a request is its own. One of `hmac-sha256-hex`, `hmac-sha256-base64`, `hmac-sha1-base64-url`, `shared-secret-header` or `url-token` |
| `header`, `prefix`, `param`, `callback_url` | where the proof is, and what the scheme needs besides |
| `key` | what events group on: one batch per key and burst |
| `dedup` | which field recognises a redelivery |
| `ignore` | which verified deliveries to acknowledge and not queue, such as pings |
| `event_type`, `attrs` | a label and values a route can match on |
| `head_handshake` | answer the sender's unsigned `HEAD` probe with 200 |
| `sender_retries` | whether the sender retries a failed delivery; `hookspool check` warns when it does not, so you remember the [sweep](../operations/sweeps.md) |

The [bobbin's README](../../hookspool/bobbins/webhook/README.md) has the full table and says how outputs map to routes
when one service sends several event types. A value the template refuses stops `hookspool check` with the template's own
message.

## When a service deserves its own bobbin

If several deployments set up the same service through `webhook`, copy it into a bobbin of its own, with the settings
frozen:

```sh
hookspool bobbin new invoices --set scheme=hmac-sha256-hex --set header=X-Invoices-Signature --set key=customer.id
```

The copy has captures signed for the scheme and `README.md`, `SETUP.md` and `AGENTS.md` to fill in. The rest of the
path is in [Writing a bobbin](writing-a-bobbin.md). If the sender signs something other than the raw body, needs a
handshake answer that is computed, or wants an API call after each event, add `--code` or `--processor` to the command.

## Limits

It reads JSON bodies, and each selector reads one value. There is no expression language, on purpose: a sender that needs
more gets a code bobbin.
