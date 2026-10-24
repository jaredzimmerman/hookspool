---
bobbin: google.meet
provider: Google Meet
webhook_docs: https://developers.google.com/workspace/events/guides/events-meet
---

# Setting up the google.meet bobbin (for a person)

You need four things:

* a Google Cloud project with the Google Meet REST API, the Google Workspace Events API and the Cloud Pub/Sub API
  enabled;
* an OAuth client, and a refresh token with the `https://www.googleapis.com/auth/meetings.space.readonly` scope for
  the account whose meetings you want to follow (a personal Google account works);
* hookspool reachable over HTTPS (see `deploy/nginx.example.conf`; the query string must reach the intake);
* for the calls below, an access token of that account with the same scope, written as `<ACCESS_TOKEN>`.

## 1. At Google

1. Enable the APIs:
   `gcloud services enable meet.googleapis.com workspaceevents.googleapis.com pubsub.googleapis.com`.
2. Create a Pub/Sub topic, for example `meet-events`, and give Meet's publisher account the Publisher role on it:
   `gcloud pubsub topics add-iam-policy-binding meet-events --member=serviceAccount:meet-api-event-push@system.gserviceaccount.com --role=roles/pubsub.publisher`.  <!-- hookspool-scan: allow: Google's documented Meet events publisher account -->
3. Create a **push** subscription on the topic:
   `gcloud pubsub subscriptions create meet-push --topic=meet-events --push-endpoint='https://<HOST>/hooks/<INSTANCE>?token=<PUSH_TOKEN>'`.
   `<PUSH_TOKEN>` is a long random string you make up (for example `openssl rand -hex 32`), and `<INSTANCE>` is
   the instance name you choose in step 3.
4. Make an OAuth credential JSON file with `client_id`, `client_secret`, `refresh_token` and, optionally,
   `token_uri`.
5. Find the account's numeric user id. The subscription target has to be
   `//cloudidentity.googleapis.com/users/<USER_ID>`, and Google refuses `users/me`. Meet tells you the number:
   hold a short call while signed in as the account, then list its conference records
   (`GET https://meet.googleapis.com/v2/conferenceRecords` with `Authorization: Bearer <ACCESS_TOKEN>`) and the
   participants of the newest one (`GET https://meet.googleapis.com/v2/<CONFERENCE_RECORD>/participants`). The
   account's own entry has `signedinUser.user = "users/<USER_ID>"`.
6. Create the Workspace Events subscription:

   ```
   POST https://workspaceevents.googleapis.com/v1/subscriptions
   Authorization: Bearer <ACCESS_TOKEN>
   Content-Type: application/json

   {"targetResource": "//cloudidentity.googleapis.com/users/<USER_ID>",
    "eventTypes": ["google.workspace.meet.conference.v2.started", "google.workspace.meet.conference.v2.ended",
                   "google.workspace.meet.participant.v2.joined", "google.workspace.meet.participant.v2.left"],
    "notificationEndpoint": {"pubsubTopic": "projects/<PROJECT>/topics/meet-events"},
    "payloadOptions": {"includeResource": false}}
   ```

   The answer is an operation. Once it is done, its `response.name` is the subscription
   (`subscriptions/<SUBSCRIPTION_ID>`). Keep that name for the renewal in step 5. A subscription lasts seven days.

## 2. Secrets

Write each value to a file readable only by the hookspool user (mode 0600):

* `/etc/hookspool/secrets/meet_push_token`: the `<PUSH_TOKEN>`;
* `/etc/hookspool/google/meet.json`: the credential JSON.

## 3. Configuration

```toml
[bobbins.meet]
bobbin = "google.meet"
secrets.push_token = { file = "/etc/hookspool/secrets/meet_push_token" }
secrets.cred = { file = "/etc/hookspool/google/meet.json" }
outputs = { meeting-started = "hermes:meet-started", meeting-ended = "hermes:meet-ended", subscription-changed = "hermes:meet-subscription" }
expect = true
```

Leave out any output you do not want; its items are dropped. Check the table and look at what it expands to:

```
hookspool check
hookspool bobbin expand meet
```

The state directory (default `<home>/bobbins/meet`) must be listed in the dispatcher unit's `ReadWritePaths`
(`deploy/hookspool-dispatch.service`) and be private (mode 0700).

## 4. Verify

1. Run `hookspool reload`.
2. Start a call in a meeting space the account owns, have a second person join, and end it within a few minutes.
3. Run `hookspool list --source meet`. After the `meeting` span (60 s quiet), it shows one batch for the call
   that ends `delivered`, and the route named for `meeting-ended` receives an item with the participants.
   `hookspool status` shows the destination `script:meet` closed.

## 5. The renewal sweep, and rollback

Pub/Sub retries a failed push for the subscription's retention, so the one real gap is a lapsed Workspace Events
subscription. Run a daily job that renews it to the longest allowed expiry:

```
PATCH https://workspaceevents.googleapis.com/v1/subscriptions/<SUBSCRIPTION_ID>?updateMask=ttl
Authorization: Bearer <ACCESS_TOKEN>
Content-Type: application/json

{"ttl": "0s"}
```

Keep `expect = true`. A week with no push opens a `source_silent` issue; a renewal sends no push, so a week with no
calls opens one too. Google also sends lifecycle events to the topic 12 hours and one hour before the expiry, and
when the subscription expires or is suspended. They arrive as `subscription-changed` items. After `expired`,
create the subscription again (step 1.6). After `suspended`, fix the reason it names, then call
`POST https://workspaceevents.googleapis.com/v1/subscriptions/<SUBSCRIPTION_ID>:reactivate`.

To roll back, delete the subscription
(`DELETE https://workspaceevents.googleapis.com/v1/subscriptions/<SUBSCRIPTION_ID>`), point the push subscription
at the previous consumer or delete it, then remove the `[bobbins.meet]` table and run `hookspool reload`. The spool
keeps what it already accepted, and the state directory is not deleted.
