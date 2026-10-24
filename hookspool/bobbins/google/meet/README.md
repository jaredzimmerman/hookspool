---
bobbin: google.meet
provider: Google Meet
webhook_docs: https://developers.google.com/workspace/events/guides/events-meet
---

# google.meet

Google Meet events, delivered by the Google Workspace Events API to a Cloud Pub/Sub topic and pushed to hookspool.
The bobbin reports when a call starts and, once it ends, who attended and when. It never asks Meet for
transcripts, recordings or chat, and no model runs here.

```
Workspace Events subscription (a user) -> Pub/Sub topic -> push subscription -> POST /hooks/<instance>?token=...
   source.py     checks the push token, decodes the CloudEvent, keys it by the conference record
   processor.py  one pass per batch: ended -> conference record + participants from Meet REST v2; otherwise started
```

The Pub/Sub push rules (the token check, the envelope decoding) are shared with the google.gmail bobbin, and the
Meet API calls (conference record, participants, participant sessions) live in `hookspool.providers.google`.

It works on a personal Google account without a Workspace plan. The subscription goes on a user, and its target is
`//cloudidentity.googleapis.com/users/<numeric id>`. Google refuses `users/me`, so SETUP.md shows how to find
the number. The subscription covers every meeting space the user owns.

## What a pass does

The source keys each event by its conference record, and the `meeting` span (60 s quiet, 900 s max) groups a
call's events. A call that starts and ends inside 15 minutes is one batch.

1. The batch holds `google.workspace.meet.conference.v2.ended`. The processor reads the conference record and
   its participants, plus each participant's sessions (for the first 100 participants), and returns one
   `meeting-ended` item.
2. The batch has no end event. The processor returns one `meeting-started` item built from the events alone (no
   API call), but only the first time it sees a batch for that conference. A call that runs past 15 minutes sends
   later batches of joins and leaves, and those report nothing, as does a stray event that arrives after the end.
3. A subscription lifecycle event (`expirationReminder`, `expired`, `suspended`) becomes one `subscription-changed`
   item.

The source verifies and acknowledges other Meet event types (recordings, transcripts, smart notes) and any type
Google adds later, but it never queues them.

## Outputs

| output | when | carries |
|---|---|---|
| `meeting-started` | the first batch of a call with no end in it | `conference_id`, `conference_record`, `space` (from the event subject, or ""), `start` (the start event's time, else the earliest event's), `joined` (participant sessions that joined in the batch) |
| `meeting-ended` | a batch with the end event | `conference_id`, `conference_record`, `space`, `start`, `end`, `participant_count`, `participants_complete`, `participants` |
| `subscription-changed` | a lifecycle event of the Workspace Events subscription | `lifecycle` (`expiration-reminder`, `expired`, `suspended`), `subscription`, `expire_time`, `state`, `suspension_reason`, `event_time` |

Each entry of `participants` is `{display_name, kind, user_id, first_join, last_leave, sessions}`. `kind` is
`signedin`, `anonymous` or `phone`. `user_id` is the numeric id of a signed-in user and "" for anyone else.
`first_join` and `last_leave` come from the participant's `earliestStartTime` and `latestEndTime`, and fall back
to its sessions when those are missing. `sessions` is the number of sessions, or null past the first 100
participants. The list is ordered by first join. Every item also carries `source` (`google-meet`) and `key`
(`meet:<conference id>`, or `subscription:<id>`), which becomes the Hermes event key.

## Secrets

| name | meaning |
|---|---|
| `push_token` | the long random token in the Pub/Sub push endpoint URL (`?token=...`). The source needs it. |
| `cred` | OAuth credential JSON (`client_id`, `client_secret`, `refresh_token`, optional `token_uri`) of the account that owns the meetings. The processor needs it. The scope `https://www.googleapis.com/auth/meetings.space.readonly` is enough. |

References must be `file` or `env_file`. The processor only reads the credential file. It keeps the access token,
and any rotated refresh token, in its own runtime state under the state directory. That state is clear text, so a
`state_dir` you set yourself must be private (mode 0700).

## Settings

| setting | type | default | meaning |
|---|---|---|---|
| `api_url` | string | Meet REST v2 base URL | change only to point at a test server |

## State

The state lives in the instance's state directory (default `<home>/bobbins/<instance>/`) as `meet_state.json`:
`{"seen": {conference record: {"state", "at"}}}`. An entry is kept for two days. The runtime writes state only
after its journal holds the result, so a retried batch reports the same items once.

## Limits

* A Workspace Events subscription lasts at most seven days, and only a renewal keeps it alive. Renew it daily
  (SETUP.md, step 5). An expired subscription is deleted and has to be created again. Set `expect = true`: a week
  without any push then opens an issue. A person who has no calls for a week sees that issue too.
* Google sends the end event a little after the call ends. Participants' `latestEndTime` comes from Meet and can
  trail the end event by a few seconds.
* Meet REST keeps a conference record for 30 days. If the record is gone (HTTP 404), the item carries the event
  times and an empty participant list.
* The participant listing reads at most 20 pages of 250. `participants_complete` is false when it stopped early.
* Personal accounts get no transcripts, and this bobbin never asks for them.
* Errors map the same way as in every bobbin: 408, 425, 429 and 5xx retry the batch (exit 75), and any other 4xx is
  permanent (exit 65, dead-lettered).
* Logs carry counts only, never names or ids of people.
