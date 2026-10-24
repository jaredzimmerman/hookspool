---
bobbin: google.meet
provider: Google Meet
webhook_docs: https://developers.google.com/workspace/events/guides/events-meet
---

# Setting up the google.meet bobbin (for an agent)

Follow these steps in order. A step marked `outward` changes something at the provider or in a shared account, so
ask the person before you run it.

## Needs from the human

* A Google Cloud project, and permission for you to enable the Google Meet REST API, the Google Workspace Events API
  and the Cloud Pub/Sub API in it and to create a topic and a subscription there. Otherwise the person runs the
  `outward` steps themselves.
* An OAuth credential JSON with `client_id`, `client_secret`, `refresh_token` and the scope
  `meetings.space.readonly`, for the account whose meetings are followed, saved on this host. Only the account's
  owner can give the consent and produce the refresh token. Never print or paste the file's contents.
* An access token of that account with the same scope, for the API calls in steps 8, 9 and 11 (`<ACCESS_TOKEN>`).
  Use it inside the command only, and do not echo it.
* One short test call, held while signed in as the account, so that its numeric user id can be read (step 8).
* The public HTTPS host name that reaches hookspool, and the Google Cloud project id.
* Which outputs (`meeting-started`, `meeting-ended`, `subscription-changed`) go to which destinations.

Secrets used below: `push_token` (made in step 2) and `cred`. There are no required settings.

## Steps

1. **Check the host.** Goal: hookspool is installed and its intake is reachable. Run `hookspool status` and expect
   an ok status. If the intake is not reachable over HTTPS, stop and ask the person: the proxy line must keep the
   query string (`deploy/nginx.example.conf`).
2. **Make the push token.** Goal: a secret that only this push subscription knows. Run
   `umask 077; openssl rand -hex 32 > <SECRETS_DIR>/meet_push_token`. Check that the mode is 0600 and the owner is
   the hookspool user (`ls -l`). Never print the content. If the mode or the owner is wrong, fix it with `chmod 600`
   and `chown`.
3. **Place the credential.** Goal: `<CRED_FILE>` is a regular file, mode 0600, owned by the hookspool user, holding
   JSON with `client_id`, `client_secret` and `refresh_token`. Check with
   `python3 -c "import json,sys; print(sorted(json.load(open(sys.argv[1]))))" <CRED_FILE>`, which prints key names
   only. If a key is missing, stop and ask the person for a correct credential. Do not try to make a refresh token
   yourself.
4. **Add the configuration.** Goal: the instance expands and checks clean. Add:

   ```toml
   [bobbins.meet]
   bobbin = "google.meet"
   secrets.push_token = { file = "<SECRETS_DIR>/meet_push_token" }
   secrets.cred = { file = "<CRED_FILE>" }
   outputs = { meeting-ended = "<DESTINATION>", subscription-changed = "<DESTINATION>" }
   expect = true
   ```

   Add `meeting-started` only if the person asked for it. Run `hookspool check` (expect exit 0) and
   `hookspool bobbin expand meet` (it shows references, never values). If check names a problem, fix the table and
   run check again.
5. **Make the state directory writable.** Goal: the dispatcher unit can write `<STATE_DIR>` (default
   `<HOME>/bobbins/meet`), mode 0700. Check that `deploy/hookspool-dispatch.service` lists it under
   `ReadWritePaths`. If it does not, tell the person: editing the unit is a system change.
6. **Reload.** Run `hookspool reload`, then check that `hookspool status` shows no config issue. If it shows one,
   fix the configuration (step 4) or the state directory (step 5) and reload again.
7. **Create the Pub/Sub topic and push subscription** (`outward`). Goal: Meet can publish to the topic, and Pub/Sub
   pushes to hookspool. Run
   `gcloud services enable meet.googleapis.com workspaceevents.googleapis.com pubsub.googleapis.com --project <PROJECT>`.
   Create the topic `<TOPIC>` and grant `meet-api-event-push@system.gserviceaccount.com` the role  <!-- hookspool-scan: allow: Google's documented Meet events publisher account -->
   `roles/pubsub.publisher` on it (`gcloud pubsub topics add-iam-policy-binding <TOPIC> --member=serviceAccount:<THAT ACCOUNT> --role=roles/pubsub.publisher`).
   Then create a push subscription on it:
   `gcloud pubsub subscriptions create <SUBSCRIPTION> --topic=<TOPIC> --push-endpoint="https://<HOST>/hooks/meet?token=$(cat <SECRETS_DIR>/meet_push_token)"`.
   Read the token from the file inside the command only. Check that
   `gcloud pubsub subscriptions describe <SUBSCRIPTION> --format='value(topic)'` names the topic. Do not print the
   push endpoint, because it holds the token. If the grant fails, the person must give
   you rights on the project.
8. **Find the numeric user id.** Goal: `<USER_ID>`, the number Google uses for the account. Google refuses
   `users/me` as a subscription target. Ask the person to hold a short call while signed in as the account (or use
   a recent one). Call `GET https://meet.googleapis.com/v2/conferenceRecords` with
   `Authorization: Bearer <ACCESS_TOKEN>`, take the newest record's `name`, then call
   `GET https://meet.googleapis.com/v2/<CONFERENCE_RECORD>/participants`. The account's own entry has
   `signedinUser.user = "users/<USER_ID>"`, so ask the person which display name is theirs. If the list is empty,
   the call was not held in a space this account owns: ask for another test call.
9. **Create the Workspace Events subscription** (`outward`). Goal: Meet events for every space the account owns
   reach the topic. Call `POST https://workspaceevents.googleapis.com/v1/subscriptions` with
   `Authorization: Bearer <ACCESS_TOKEN>` and the body
   `{"targetResource": "//cloudidentity.googleapis.com/users/<USER_ID>", "eventTypes": ["google.workspace.meet.conference.v2.started", "google.workspace.meet.conference.v2.ended", "google.workspace.meet.participant.v2.joined", "google.workspace.meet.participant.v2.left"], "notificationEndpoint": {"pubsubTopic": "projects/<PROJECT>/topics/<TOPIC>"}, "payloadOptions": {"includeResource": false}}`.
   The answer is an operation. Check that it is `done` and that `response.name` is `subscriptions/<SUBSCRIPTION_ID>`,
   and keep that name for step 11. If the answer is 403, the scope or the API is missing, and the person must fix
   the consent or the project. A 400 that names the target means the user id is wrong, so repeat step 8. A
   permission error on the topic means the publisher grant in step 7 is missing.
10. **Verify.** Goal: a real call is received and processed. Ask the person to start a call in a space the account
    owns, let someone join, and end it within a few minutes. Check that `hookspool list --source meet` shows a batch
    that ends `delivered` after about a minute. `retry` means a transient error, which
    `hookspool show <BATCH_ID> --attempts` explains. `dead` with `exit 65` means Google refused the credential or
    the request: tell the person. A `401` line in the intake log means the push endpoint token differs from the
    token file. Optionally, `hookspool source test meet --request <FILE>` checks the request path offline. It
    queues nothing, and it verifies the request against the configured token, so `<FILE>` must be a capture that
    carries the real token (the shipped `captures/conference_ended.http` carries a fixture token and reports
    `unverified`). Only a real call proves delivery.
11. **Renew the subscription daily** (`outward`: installs a recurring job). Goal: the subscription never lapses,
    since it lasts at most seven days. Schedule a daily job that calls
    `PATCH https://workspaceevents.googleapis.com/v1/subscriptions/<SUBSCRIPTION_ID>?updateMask=ttl` with
    `Authorization: Bearer <ACCESS_TOKEN>` and the body `{"ttl": "0s"}`, which renews to the longest expiry allowed.
    Check that the answer's `expireTime` (or the finished operation's `response.expireTime`) is about seven days
    away. If the call answers 404, the subscription has expired and been deleted, so repeat step 9. A
    `subscription-changed` item with `lifecycle = suspended` names a `suspension_reason`: fix the cause, then call
    `POST https://workspaceevents.googleapis.com/v1/subscriptions/<SUBSCRIPTION_ID>:reactivate`.

Rollback: delete the subscription (`DELETE https://workspaceevents.googleapis.com/v1/subscriptions/<SUBSCRIPTION_ID>`),
delete the push subscription or point it back at the previous consumer, remove the `[bobbins.meet]` table, and run
`hookspool reload`. Nothing in the spool is lost.
