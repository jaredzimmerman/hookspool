---
bobbin: google.drive
provider: Google Drive
webhook_docs: https://developers.google.com/workspace/drive/api/guides/push
---

# google.drive

Google Drive push (a watch channel on the change feed): a push says that something changed, the processor reads
what changed from the feed's stored page token and reports which files were changed, created or shared with the
account. It reports identity and metadata only (id, name, type, time, who, link); it never reads file contents.
No model runs here.

```
Drive changes.watch -> POST /hooks/<instance> (empty body, X-Goog-* headers)
   source.py     verifies the channel token, builds the payload from the headers, keys it by the watched resource
   processor.py  one pass per batch: changes since the stored page token -> filter -> file-* items
```

The channel rules (token check, `sync` handshake, header payload, dedup id) and the Drive API calls (start page
token, change paging, the "page token gone" answer) are `hookspool.providers.google`'s; the channel rules are shared
with the google.calendar bobbin.

## What a pass does

1. Reads the feed from the stored page token, following `nextPageToken` (up to 50 pages of 1000 changes), and
   stores the feed's new start page token. If the cap is reached first, it stores the page it stopped at and the
   next push carries on from there.
2. Keeps the last change of each file and drops: removals (there is nothing left to describe), files in the trash
   (unless `include_trashed` is on), MIME types in `skip_mime_types`, and changes made by one of the `own` accounts.
3. Names what is left. `file-shared`: the file reached the account after the previous pass (`sharedWithMeTime` is
   newer than the newest change that pass saw); `file-created`: its `createdTime` is; anything else is
   `file-changed` (edited, renamed, moved, untrashed). A file that is both shared with the account and new is
   `file-shared`: that is the part someone has to act on.

The first push, a `sync` ping, or a page token Drive rejects (HTTP 404 or 410) only stores a fresh start page
token and reports nothing: changes made before that point are not reported.

## Outputs

| output | when | carries |
|---|---|---|
| `file-changed` | an existing file was changed by someone other than an `own` account | the fields below |
| `file-created` | a new file reached the feed | the fields below |
| `file-shared` | a file was shared with the account | the fields below; `actor` is the person who shared it |

Every item carries: `file_id`, `name`, `mime_type`, `modified_time`, `actor` (the email address of the last editor,
or of the sharer for `file-shared`; empty when Drive does not say), `web_link`, `change_time`, `source`
(`google-drive`) and `key` (`file:<file id>`, which becomes the Hermes event key). No file contents, no
permissions list.

## Secrets

| name | meaning |
|---|---|
| `channel_token` | the token given when the watch channel was created, echoed by Google in `X-Goog-Channel-Token`. Needed by the source. |
| `cred` | OAuth credential JSON (`client_id`, `client_secret`, `refresh_token`, optional `token_uri`). Needed by the processor. The scope `https://www.googleapis.com/auth/drive.metadata.readonly` is enough. |

References must be `file` or `env_file`. The credential file is only read: the processor keeps its access token
and any rotated refresh token in its own runtime state under the state directory (clear text, so a `state_dir`
you set yourself must be private, mode 0700).

## Settings

| setting | type | default | meaning |
|---|---|---|---|
| `own` | list | `[]` | addresses that are "us": changes they make are dropped (matched against the last editor, lower-cased) |
| `skip_mime_types` | list | `[]` | MIME types to ignore: exact (`application/vnd.google-apps.folder`) or a family (`image/*`) |
| `include_trashed` | bool | `false` | also report files in the trash (as `file-changed`) |
| `drive_id` | string | `""` | id of one shared drive to watch; empty means the account's own change feed (which includes files shared with it) |
| `api_url` | string | Drive v3 base URL | change only to use a test server |

## State

Under the instance's state directory (default `<home>/bobbins/<instance>/`): `drive_push_state.json`
`{pageToken, high_water, bootstrapped}`. The runtime writes state only after its journal holds the result: a
retried batch reports the same items and advances the page token once. To start again from "now", stop the
dispatcher's use of the instance, delete that file and let the next push bootstrap.

## Limits

* A pass reads at most 50 pages of 1000 changes. If the cap is hit, the rest waits for the next push (logged
  "more pending"); on a quiet Drive that can be days.
* A watch channel expires; Google does not warn. Renew it daily (SETUP.md) and set `expect = true` so silence
  opens an issue (default: a week without a push; a quiet Drive can be silent for days).
* Drive reports a change per file save, so a file being edited produces many pushes; the `calendar` span (45 s
  quiet, 180 s max) turns them into few passes, and one item per file per pass.
* Errors map as everywhere: 408, 425, 429 and 5xx retry the batch (exit 75); another 4xx is permanent (exit 65,
  dead-lettered). A rejected page token other than 404 or 410 is therefore permanent: delete the state file to
  recover.
* Logs carry counts only, never names or addresses.
