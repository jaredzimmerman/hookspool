---
bobbin: google.contacts
provider: Google Contacts
api_docs: https://developers.google.com/people/api/rest/v1/people.connections/list
---

# google.contacts

Google Contacts, polled. The People API cannot push (it has no webhook and no watch channel), so this is a poll
bobbin. Every `every_s` seconds (default 900) the dispatcher's clock starts one pass, and the pass asks Google
which contacts changed since the sync token it stored last time. If any did, it emits one `contacts-changed` item.
No model is involved.

The item says which contacts changed and nothing about them. It carries resource names (`people/c123`) and
counts, never a name, an email address or a phone number. The request asks Google for the `metadata` field only, so
no personal data reaches this process, its logs, its state or the spool. A deployment's own job reads the details it
needs for the resource names listed.

```
dispatcher clock -> tick (every_s + jitter) -> poller.py
   people.connections.list  (syncToken)  -> changed / deleted resource names
   otherContacts.list       (syncToken)  -> the same, when include_other_contacts is on
```

`hookspool.providers.google` makes the People API calls (the listing, the sync token, the "token expired"
answer). The runtime handles HTTP, the OAuth refresh, retries and timeouts.

## What a pass does

1. **First pass.** No sync token is stored yet, so it reads the whole list once (resource names only, a page of
   `page_size` at a time), stores the sync token Google returns on the last page, and emits nothing. Contacts
   that changed before that point are not reported. The same happens for "Other contacts" the first time
   `include_other_contacts` is on.
2. **Later passes.** It sends the stored sync token. Google returns the contacts changed since then, and a deleted
   contact comes back with `metadata.deleted` set. The pass emits one `contacts-changed` item when there is at least
   one changed or deleted contact, stores the new token, and emits nothing when there is none.
3. **Expired token.** Google expires a sync token seven days after the full read it came from. When it refuses the
   token (HTTP 410, reason `EXPIRED_SYNC_TOKEN`), the pass reads the whole list again, stores the new token and emits
   an item with `full_resync` true. This happens about once a week. Google's reference names the reason but not the
   status code, so a 400 answer to a request that carried a token is handled the same way.

## Outputs

| output | when | carries |
|---|---|---|
| `contacts-changed` | at least one contact was added, edited or deleted since the last pass, or the list was read in full again | the fields below |

| field | meaning |
|---|---|
| `changed` | resource names that were added or edited (`people/...`; `otherContacts/...` for the other list) |
| `deleted` | resource names that were deleted |
| `full_resync` | true when Google refused the stored token and the whole list was read again: `changed` is then every contact that exists now and `deleted` is empty, so a consumer should drop anything it holds that is not in `changed` |
| `total_changed` | the number of names in `changed` and `deleted` together |

With `include_other_contacts` on, each list reports on its own. A pass can emit two items, one for `people/...`
names and one for `otherContacts/...` names, and `full_resync` on one says nothing about the other. Items carry no
`key`, so they go out under the batch key `poll`.

## Secrets

| name | meaning |
|---|---|
| `cred` | OAuth credential JSON (`client_id`, `client_secret`, `refresh_token`, optional `token_uri`). The scope `https://www.googleapis.com/auth/contacts.readonly` is enough; `include_other_contacts` also needs `https://www.googleapis.com/auth/contacts.other.readonly`. |

References must be `file` or `env_file`. The poller only reads the credential file. It keeps its access token and any
rotated refresh token in its own runtime state under the state directory, in clear text, so a `state_dir` you set
yourself must be private (mode 0700).

## Settings

| setting | type | default | meaning |
|---|---|---|---|
| `page_size` | int | `1000` | contacts per request, 1 to 1000. Google wants a sync token used with the page size it was issued with, so a stored token keeps its page size until the next full read. |
| `include_other_contacts` | bool | `false` | also follow "Other contacts" (people the account has only corresponded with), with a sync token of its own |
| `api_url` | string | People API v1 base URL | change only to use a test server |

The poll interval is the instance's `every_s` (default 900, at least 60) and `jitter_s`, as for any poll bobbin.

## State

Under the instance's state directory (default `<home>/bobbins/<instance>/`): `contacts_sync.json`
`{"connections": {"token", "page_size"}, "other": {"token", "page_size"}}`. The sync tokens are opaque strings and
are never logged. The runtime writes state only after its journal holds the result, so a retried batch emits the
same item and moves each token once. To start again from "now", stop the instance, delete that file and let the
next tick bootstrap. Turning `include_other_contacts` off drops that list's token on the next pass.

## Limits

* A pass reads at most 100 pages of `page_size` contacts for each list (100 000 contacts at the default). A longer
  list stops the pass with a permanent error, because Google sends a sync token only with the last page.
* Google says writes can take several minutes to appear in an incremental sync, which is not meant for
  read-after-write. A change shows up a tick or two later at the earliest.
* The first page of a full read has a separate quota. When it is spent Google answers 429 and the pass retries
  with the dispatcher's backoff.
* A poll gives up the sender's retries: a change is found only when a tick runs. `hookspool status` shows each
  poll under `polls`, and an instance with no delivered tick for three intervals opens a `poll:<instance>:stale`
  issue.
* Errors map as everywhere: 408, 425, 429 and 5xx retry the batch (exit 75); another 4xx is permanent (exit 65,
  dead-lettered).
* Log lines say how many names changed and were deleted. They never include a name, a token or a resource name.
