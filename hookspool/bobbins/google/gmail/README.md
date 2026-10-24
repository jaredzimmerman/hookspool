---
bobbin: google.gmail
provider: Google Gmail
webhook_docs: https://developers.google.com/workspace/gmail/api/guides/push
---

# google.gmail

Gmail push through Cloud Pub/Sub: a push says that a mailbox changed, the processor reads what is new
from Gmail's history, and reports the mail the agent should see. No model runs here: the decisions are
rules, settings and an optional hooks module.

```
Gmail users.watch -> Pub/Sub topic -> push subscription -> POST /hooks/<instance>?token=...
   source.py   verifies the token, normalises the notification, keys it by mailbox (one batch per mailbox)
   processor.py   per mailbox: history since the stored id -> messages -> filters -> items
```

## What it reports (outputs)

| output | when | carries |
|---|---|---|
| `scheduling` | new inbox mail in the **owner's** mailbox that asks for the owner's availability (matched on the subject and the first 4000 characters of the text) | `account`, `message_id`, `thread_id`, `rfc_message_id`, `from`, `from_address`, `from_domain`, `subject`, `received`, `excerpt` (first 1500 characters), `free_windows_text` (the owner's free working-hours windows over the next 7 days, weekdays only, from free/busy) |
| `agent-mail` | new inbox mail in the **assistant's** mailbox (the `agent` setting) | the same fields, without free windows |
| `agent-schedule` | a message a deployment's `hooks.classify` hands off (see below) | whatever the hook returns, plus the standard fields and `free_windows_text` (next 14 days, weekdays only) |
| `mailbox-changed` | a pass read new mail in a mailbox; one per mailbox per batch | `account`, `new_messages`, `history_id` |

Every item has a `key` (`<account>:<thread>`, or the mailbox for `mailbox-changed`) that becomes the
Hermes event key, so each thread is its own event. Map the outputs you want in the instance's `outputs`
table; an output you leave out is dropped with one log line per batch. Every request of a coalesced batch
is reported, not only the first.

Mail is skipped (never reported as `scheduling` or `agent-mail`) when it is not in the inbox, carries a
promotions/social/forums/spam/trash label, is from the owner, the assistant or an `own_domains` domain,
is from a `skip_domains` sender, looks like a no-reply sender, or has a `skip_keywords` word in the subject
(whole words only: `sale` does not skip "Salesforce"; subjects with recruiting words such as `interview`,
`offer` or `next steps` are never skipped on a keyword). Mail whose `To`/`Cc` includes the assistant is left
to the assistant. `important_senders` (addresses, or domains with or without a leading `@`) are never
skipped by `skip_domains`, `skip_keywords` or the no-reply rule.

## Secrets

| name | meaning |
|---|---|
| `push_token` | the long random token in the push endpoint URL (`?token=...`), compared in constant time. Needed by the source. |
| `cred_owner` | OAuth credential JSON for the owner's mailbox: `client_id`, `client_secret`, `refresh_token`, optional `token_uri`. Needed by the processor; also used for the owner's free/busy. |
| `cred_agent` | the same for the assistant's mailbox. Optional: leave it out when there is no `agent` setting. |

References must be `file` or `env_file` (the processor does not inherit the dispatcher's environment).
The credential files are only read: the processor keeps its access token and any rotated refresh token in
its own runtime state, under the state directory, never in the credential file. That state holds tokens in
the clear, so a `state_dir` you set yourself must be private (mode 0700, owned by the service user).

## Settings

| setting | type | default | meaning |
|---|---|---|---|
| `owner` | string, required | | the owner's mailbox address |
| `agent` | string | `""` | the assistant's own mailbox address |
| `agent_state` | string | `gmail_push_state_agent` | name of the assistant mailbox's state file (without `.json`); lets an existing state carry over |
| `tz` | string | `UTC` | time zone for free windows and message times |
| `hours` | string | `9:00-17:30` | working window for proposed times (24-hour; an unreadable value falls back to the default) |
| `own_domains` | list | `[]` | domains whose mail is "from us" |
| `skip_domains` | list | `[]` | sender domains (and subdomains) never reported; an entry with `@` in it is one exact address |
| `skip_keywords` | list | `[]` | whole words that keep a subject from being reported |
| `important_senders` | list | `[]` | never skipped by the three skip rules |
| `api_url`, `freebusy_url` | string | the Google URLs | change only to point at a test server |
| `hooks` | path | `""` | absolute path of a hooks module, see below |

## Hooks

A deployment never edits this bobbin. To hand some messages off privately, set `hooks` to the absolute
path of a Python file (a regular file, not group- or other-writable, owned by root or the service user)
that defines

```python
def classify(message: dict, account: str, ctx) -> dict | None:
    """Return None to let the bobbin's own rules look at the message, or a complete item."""
```

`message` is the parsed message: `{"id", "thread_id", "labels": [...], "headers": {lower-cased name: first
value}, "body": text of the first text/plain part (or the snippet), "snippet", "internal_date": ms since the
epoch}`. `account` is the mailbox the message arrived in. `ctx` is the processor context (`ctx.settings`,
`ctx.http`, `ctx.load_state`, ...). It is called once for each new message that has the `INBOX` label, in
every watched mailbox, before the bobbin's own rules (including the skip rules).

The returned dict must have an `event_type` that is one of this bobbin's outputs (use `agent-schedule` for a
private hand-off); anything else stops the batch as a permanent failure, so a typo cannot silently drop mail.
The bobbin adds `account`, `source` (`gmail`), `message_id`, `thread_id`, `rfc_message_id` and `key`
(`<account>:<tracked_thread_id or thread_id>`) unless the hook set them, and `free_windows_text` to an
`agent-schedule` item. A hook that raises retries the batch; nothing moves. Hooks are deployment code and are
not subject to the rules that keep a bobbin's own modules small.

## State

Under the instance's state directory (default `<home>/bobbins/<instance>/`): `gmail_push_state.json` for the
owner and `gmail_push_state_agent.json` (or `agent_state`) for the assistant, each `{historyId, handled,
bootstrapped}`. The runtime writes them only after its journal holds the result, so a retried batch loses
nothing and advances a history id once. The first push for a mailbox (or one whose history id Gmail has
dropped, HTTP 404) only records Gmail's current history id; nothing is reported for it.

## Limits

* One pass reads at most 20 pages of 500 history records (10,000). If a mailbox changed by more than that
  between two passes, the rest of that gap is not read: the pass stores Gmail's current history id and logs
  how many pages and records it read.
* `agent` may not be the owner's mailbox: the batch is refused as permanent (exit 65).
* The `handled` list keeps the last 300 message ids.
* A Gmail watch lasts seven days: renew it (a `users.watch` call) daily; see SETUP.md for the sweep.
* HTTP errors map as everywhere in hookspool: 408, 425, 429 and 5xx retry the batch (exit 75); another 4xx,
  including a revoked credential, is permanent (exit 65): the batch is dead-lettered and an issue opens.
* Logs carry counts and the label `owner` or `agent` only, never subjects, addresses or bodies.

## Moving from 0.1

This bobbin replaces the 0.1 delta script. [Upgrading to 0.2](../../../docs/operations/upgrading.md#upgrading-to-02-bobbins)
maps each of its variables to a setting and keeps the history ids by pointing `state_dir` at the old directory.
