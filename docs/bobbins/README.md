# Bobbins

A bobbin is the plugin for one outside service. It holds what hookspool needs to take that service's webhooks and turn
them into work: how the sender proves itself, what its events group on, and sometimes a processor that asks the service
what changed. A deployment turns one on with a `[bobbins.<instance>]` table in the config; see
[the configuration reference](../configuration/README.md#bobbinsinstance).

## The catalogue

Each name links to the bobbin's own README, which lists its settings, secrets, outputs and limits. `SETUP.md` in the
same directory is the step-by-step setup for a person, and `AGENTS.md` is the same procedure for an agent.

| bobbin | service | what it does |
|---|---|---|
| [`drive`](../../hookspool/bobbins/drive/README.md) | Google Drive | change-feed push; reports changed, created and shared files by id and metadata, never contents |
| [`gcal`](../../hookspool/bobbins/gcal/README.md) | Google Calendar | calendar push; reports conflicts, brief requests and changed events |
| [`github`](../../hookspool/bobbins/github/README.md) | GitHub | repository webhook, HMAC-SHA256 over the body; pings are acknowledged and not queued |
| [`gmail`](../../hookspool/bobbins/gmail/README.md) | Gmail | Cloud Pub/Sub push; new mail in the owner's and an optional assistant's mailbox, read from Gmail's history |
| [`kaneo`](../../hookspool/bobbins/kaneo/README.md) | Kaneo | project and task events, HMAC-SHA256 of the JSON body |
| [`plaid`](../../hookspool/bobbins/plaid/README.md) | Plaid | items, transactions and link events, signed with an ES256 JWT checked against Plaid's published keys |
| [`trello`](../../hookspool/bobbins/trello/README.md) | Trello | board webhook, HMAC-SHA1 over the body and the callback URL; answers the unsigned `HEAD` handshake |
| [`webhook`](../../hookspool/bobbins/webhook/README.md) | any web service | a signed or tokened JSON POST, set up from the sender's documentation by settings alone |
| [`zoom`](../../hookspool/bobbins/zoom/README.md) | Zoom | meeting and webinar events with a timestamped HMAC; the endpoint validation is answered after the signature verifies |

`hookspool bobbin list` prints the same list from the installed copy, and `hookspool bobbin show <bobbin>` prints one
bobbin's settings.

## In this folder

| page | what is in it |
|---|---|
| [Writing a bobbin](writing-a-bobbin.md) | the layout, the scaffold, the source (config template or code plugin), captures and the checks |
| [The webhook bobbin](webhook.md) | covering a service that has no bobbin of its own, and turning that into a dedicated bobbin |
| [Private bobbins and hooks](private-bobbins.md) | changing behaviour without editing a built-in bobbin |
| [Reviewing a bobbin](review.md) | `hookspool bobbin check`, what green means, and what a maintainer still checks |
