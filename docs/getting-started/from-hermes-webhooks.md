# Moving from Hermes webhooks

You already run a Hermes Agent gateway that takes webhooks directly: each sender posts to a route such as
`/webhooks/github-ci`, and the gateway checks the signature and wakes the agent. This page moves that setup to hookspool
without touching the senders or the Hermes routes' prompts. The gateway keeps running and waking the agent. The difference is that a request is verified and written to disk first, retried
until the gateway accepts it, and grouped if you choose.

The first pass changes no sender URL and no prompt. Each event reaches its route exactly as the
sender sent it, one request per event, so a route's prompt template and its `events` filter keep working. Coalescing is a separate second step ([After it runs](#after-it-runs)).

You need hookspool installed on the host that runs the gateway ([Install](install.md)), the reverse proxy config that
fronts the gateway, and the secret each sender uses. Do one route at a time; the others keep working untouched.

## 1. Install next to the gateway

Follow [Running hookspool](../operations/running.md) for the service units. In `config.toml`, tell hookspool where the
gateway is:

```toml
[hermes]
base_url = "http://127.0.0.1:8644"
subscriptions_path = "~/.hermes/webhook_subscriptions.json"
```

* `base_url` is the gateway's webhook listener, on the same host. hookspool delivers to it directly, never through the
  public proxy.
* `subscriptions_path` is the gateway's own routes file. hookspool reads each route's secret and profile from it on every
  attempt, so a rotation needs no restart and the Hermes side stays the one place those live. A route bound to a profile is
  delivered to `/p/<profile>/webhooks/<route>`, the path the gateway expects.

Both values shown are the defaults. Nothing is moved yet: the gateway still takes every request, because no sender or proxy
points at hookspool.

## 2. Describe each route as an instance

List the routes you have:

```sh
hermes webhook list
```

For each one, add an instance of the sender's bobbin (the plugin for that service, see [Bobbins](../bobbins/README.md)):

* `github.repo` for GitHub, or the bobbin for the sender if there is one (`hookspool bobbin list`);
* `hookspool.webhook` for any other sender that signs a JSON POST ([the guide](../bobbins/webhook.md)).

Each instance needs:

| line | why |
|---|---|
| `bobbin = "<vendor>.<product>"` | the sender's rules for proving a request is genuine |
| `secrets.secret = { file = "..." }` | the sender's existing secret, as a file. It is the same value the Hermes route holds |
| `span = "immediate"` | no waiting and no grouping: each event is its own delivery, in order |
| `outputs = { "*" = "hermes:<route>" }` | send everything to the gateway route of that name |
| `[destinations."hermes:<route>"]` with `unwrap = true` | deliver the original payload, one request per event, instead of hookspool's batch document |

The secret file is written by the person who owns the secret. One way that never prints it, run by that person:

```sh
( umask 077; jq -r '."github-ci".secret' ~/.hermes/webhook_subscriptions.json | sudo tee /etc/hookspool/secrets/github-ci > /dev/null )
```

### Worked example: a GitHub CI route

The gateway route `github-ci` receives GitHub's `workflow_run` events. It keeps its own `events` filter and its prompt
template (which reads fields such as `{workflow_run.conclusion}`). The matching hookspool config:

```toml
[bobbins.github-ci]
bobbin = "github.repo"
secrets.secret = { file = "/etc/hookspool/secrets/github-ci" }
span = "immediate"
outputs = { "*" = "hermes:github-ci" }

[destinations."hermes:github-ci"]
unwrap = true
```

The GitHub bobbin already tells hookspool to carry the event type in `X-GitHub-Event`, so the route's `events` filter sees
what it saw before. Its setup steps are in the [bobbin's AGENTS.md](../../hookspool/bobbins/github/repo/AGENTS.md).

### Worked example: a sender without a bobbin

A service that signs its body with HMAC-SHA256 and sends the result in `X-Invoices-Signature`, with the Hermes route
`invoices`. The sender's own documentation gives the scheme and header:

```toml
[bobbins.invoices]
bobbin = "hookspool.webhook"
secrets.secret = { file = "/etc/hookspool/secrets/invoices" }
settings = { scheme = "hmac-sha256-hex", header = "X-Invoices-Signature", prefix = "sha256=", key = ["customer.id"], event_type = "type", dedup = "id", sender_retries = true }
span = "immediate"
outputs = { "*" = "hermes:invoices" }

[destinations."hermes:invoices"]
unwrap = true
event_header = "X-Invoices-Event"
```

`key` is what events are ordered on (here, one customer's events stay in order). `event_type` reads the event's type from
the payload, and `event_header` sends it to the gateway so a route `events` filter keeps working. Leave `event_header` out
if the route has no such filter.

## 3. Check it before anything points at it

```sh
hookspool check
hookspool bobbin expand github-ci
```

`check` must print `ok` (a warning that GitHub does not retry is expected; step 5 deals with it). `bobbin expand` prints the
ordinary tables the instance becomes, with the secret as a reference and never its value. Then load it and send one signed
request straight to the intake, on the same host:

```sh
hookspool reload
body='{"repository":{"full_name":"example/widgets"},"action":"completed","workflow_run":{"name":"test run","conclusion":"success"}}'
sig=$(printf '%s' "$body" | python3 -c 'import hmac,hashlib,sys; k=open(sys.argv[1],"rb").read().rstrip(b"\n"); print(hmac.new(k,sys.stdin.buffer.read(),hashlib.sha256).hexdigest())' /etc/hookspool/secrets/github-ci)
curl -s -X POST http://127.0.0.1:8650/hooks/github-ci -H "X-Hub-Signature-256: sha256=$sig" -H "X-GitHub-Event: workflow_run" -H "X-GitHub-Delivery: test-1" -d "$body"
hookspool list --source github-ci
```

The reply is `{"status":"queued"}`, and the batch then moves to `delivered`. The request reaches the real route, so the agent
sees a `workflow_run` event; use a body its prompt will treat as ordinary, or rehearse on a scratch route first (see
[Drills](../operations/drills.md)). If the gateway answers `duplicate`, `ignored` or `coalesced`, that is its own filter or
deduplication at work, not a fault. If the batch does not deliver, `hookspool issues` says why; the usual causes are a secret
file that differs from the route's secret and a route name that is not in the subscriptions file.

## 4. Switch at the reverse proxy

Until now senders reach the gateway through your proxy. Change the one location for the route so it forwards to the
intake instead. For nginx, using the exact-match form from
[`deploy/nginx.example.conf`](../../deploy/nginx.example.conf), whose `hs_github` rate-limit zone the example reuses:

```nginx
# before
location = /webhooks/github-ci {
    proxy_pass http://127.0.0.1:8644;
}

# after
location = /webhooks/github-ci {
    limit_req zone=hs_github burst=60 nodelay;
    proxy_pass http://127.0.0.1:8650/hooks/github-ci;
}
```

A route bound to a profile is `location = /p/<profile>/webhooks/<route>`, forwarded the same way. The senders keep the URL
they have. Copy the example file's body-size, buffering and header settings into the server block too: the intake needs a
`Content-Length`, so the proxy must buffer the request.

Reload the proxy (`nginx -t && systemctl reload nginx`). Then ask the sender for a real delivery, or redeliver a recent one,
and confirm `hookspool list --source github-ci` shows it and the sender's delivery log shows a 200.

Rollback is the same edit in reverse: point the location back at the gateway. Anything hookspool already accepted is
still delivered to the gateway, so a rollback loses nothing.

## 5. Senders that do not retry

hookspool commits an event before the sender sees its 2xx. A sender that does not retry still loses what never reached
hookspool, for example during a proxy restart. GitHub is the common one. Install the redelivery sweep so those are redelivered
from GitHub's side and deduplicated: see [Sweeps](../operations/sweeps.md) and the GitHub bobbin's setup steps. Set
`expect_every_s` on a sender that is rarely silent, so a broken path opens an issue.

## After it runs

Leave the first pass running for a few days, then go route by route.

Coalesce bursts. A push that triggers twenty checks wakes the agent twenty times in the first pass. To get one wake-up
per burst, remove `unwrap` from the destination, return the instance to the bobbin's own span, and change the prompt to read
the batch:

```toml
[bobbins.github-ci]
bobbin = "github.repo"
secrets.secret = { file = "/etc/hookspool/secrets/github-ci" }
span = "burst"
outputs = { "*" = "hermes:github-ci" }
```

The route then receives one document per burst: the original events are `events`, with `count`, `summary` and
`event_types` beside them, so a prompt can say "{summary}" and look at the list. Also delete the route's own `events` and
`coalesce` entries from the subscriptions file. `hookspool check` fails while they are there, because a gateway filter
looks at a whole batch as if it were one event, and the gateway's own coalescing keeps only the last event. Filter by event
type in hookspool instead (an `outputs` entry per type, or a [route](../configuration/README.md#routes)) and let the batch
carry the rest.

Wake cron jobs instead of polling. A tee route sends a copy of each matching event to a second destination, without
changing where the original goes. A script that starts a job once per burst replaces a poll:

```toml
[[routes]]
name = "github-ci-wake"
source = "github-ci"
tee = true
destination = "script:wake-ci"
quiet_s = 120
max_s = 600

[destinations."script:wake-ci"]
argv = ["/usr/local/bin/run-job", "ci-check"]
```

Local bridges and scripts. Anything on the host that posted to the gateway's `/webhooks/<route>` should now go through
the spool, so its events get the same retries. A script on the same host can use `hookspool enqueue --source github-ci --key example/widgets --file payload.json`, which needs no signature. A sender off the host posts a signed request to
`/hooks/github-ci` like any other.

Once every route has moved and run clean, the old proxy locations are gone and only hookspool posts to the gateway.
