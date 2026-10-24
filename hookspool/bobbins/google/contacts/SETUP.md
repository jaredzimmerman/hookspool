---
bobbin: google.contacts
provider: Google Contacts
api_docs: https://developers.google.com/people/api/rest/v1/people.connections/list
---

# Setting up the google.contacts bobbin (for a person)

The People API cannot push, so you create nothing at Google except access: no webhook, no watch channel and no
public address. hookspool asks Google for changes on a timer. You need a Google Cloud project with the People API
enabled, an OAuth client, and a refresh token for the account whose contacts you want to follow (scope
`https://www.googleapis.com/auth/contacts.readonly`, plus `https://www.googleapis.com/auth/contacts.other.readonly`
if you will turn on `include_other_contacts`).

## 1. Secret

Write the credential to a file readable only by the hookspool user (mode 0600):

* `/etc/hookspool/google/owner.json`: the OAuth credential JSON with `client_id`, `client_secret`, `refresh_token`
  and optionally `token_uri`.

## 2. Configuration

```toml
[bobbins.contacts]
bobbin = "google.contacts"
secrets.cred = { file = "/etc/hookspool/google/owner.json" }
outputs = { contacts-changed = "hermes:contacts-changed" }
```

The poll runs every 900 seconds. To change that, add `every_s = 300` (at least 60). To follow "Other contacts" as well,
add `settings = { include_other_contacts = true }` once the credential has the second scope. Then:

```
hookspool check
hookspool bobbin expand contacts
```

The state directory (default `<home>/bobbins/contacts`) must be in the dispatcher unit's `ReadWritePaths` and
private (mode 0700). It holds the sync tokens.

## 3. Verify

1. `hookspool reload`.
2. `hookspool status` shows `polls.contacts` with a `next_at`. The first tick comes within the jitter, about 90
   seconds. It reads the whole list, stores the sync token and reports nothing, so changes made before it are not
   reported.
3. Change a contact in Google Contacts, then wait for two intervals (Google can take a few minutes to show a
   write to an incremental sync).
4. `hookspool list --source contacts` shows a batch that ends `delivered`, and the destination has received one
   `contacts-changed` item. The item lists resource names such as `people/c123` and counts. It holds no name,
   address or number; your own job reads those from the People API when it needs them.

## 4. What to expect, and rollback

About once a week Google expires the sync token. The next tick then reads the whole list again and emits one item
with `full_resync` true. Up to 2000 contacts, `changed` is every contact that exists; above that the item has
`truncated` true, empty lists and the real count in `total_changed`. A job that mirrors the address book should
read either as "read everything again", not as a burst of edits. The same `truncated` item appears after a bulk
import of more than 2000 contacts.

If you see a `full_resync` item on every tick instead of about once a week, Google is rejecting the request with a
400 that is not an expired token. Check the credential's scope and the People API quota.

The dispatcher retries a tick that fails, with backoff. If no tick is delivered for three intervals,
`hookspool issues` shows `poll:contacts:stale`. To roll back, remove the `[bobbins.contacts]` table and run
`hookspool reload`. The spool keeps what it accepted; delete the state directory to forget the sync tokens.
