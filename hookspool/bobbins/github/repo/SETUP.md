---
bobbin: github.repo
provider: GitHub Repository webhooks
webhook_docs: https://docs.github.com/en/webhooks/webhook-events-and-payloads
---

# GitHub webhook setup

The GitHub bobbin receives repository webhook deliveries. Each webhook has its own secret; use one
instance per webhook.

## What to create

1. Choose the webhook's purpose and name the instance: for example `github-ci` and `github-prs`.
2. Generate a random secret, for example `openssl rand -hex 32`. Keep it private.
3. Choose the public callback for the instance, for example `https://hooks.example.test/hooks/github-ci`.

## Secrets to save

- Write the secret to `/etc/hookspool/secrets/github-ci` (and `github-prs` for the second instance), mode
  0600, owned by the hookspool user.

## Config table

```toml
[bobbins.github-ci]
bobbin = "github.repo"
secrets.secret = { file = "/etc/hookspool/secrets/github-ci" }
outputs = { "*" = "hermes:github-ci" }

[bobbins.github-prs]
bobbin = "github.repo"
secrets.secret = { file = "/etc/hookspool/secrets/github-prs" }
outputs = { "*" = "hermes:github-prs" }
```

## Reverse proxy

Pass `/hooks/github-ci` and `/hooks/github-prs` through with the path unchanged. GitHub sends no query
string.

## Apply, then create the webhook

1. `hookspool bobbin expand github-ci` prints the source with the secret as a reference only.
2. `hookspool check`, then `hookspool reload`. Do this before creating the webhook, so GitHub's first ping
   is answered.
3. In the repository, go to Settings, Webhooks, Add webhook. Set the payload URL to the callback from step 3,
   set content type to `application/json`, paste the secret from `/etc/hookspool/secrets/github-ci`, and
   choose the events to send. Save.

## Verify

1. GitHub sends a `ping` when the webhook is created. In the webhook's recent deliveries, redeliver it: it
   is acknowledged with 200 and `hookspool list --source github-ci` shows no event for it.
2. Push a commit to the repository. `hookspool list --source github-ci` shows a queued event.

## Sweep (GitHub does not retry)

GitHub does not retry a failed delivery, so a daily sweep redelivers the failures of the last day. Run it
as the operator, with the GitHub CLI logged in as a user who can administer the repository. Replace
`<OWNER>`, `<REPO>` and `<INSTANCE>` with the values for the instance.

```sh
# 1. the hook id of the instance (the webhook whose URL ends in /hooks/<INSTANCE>)
HOOK_ID=$(gh api "repos/<OWNER>/<REPO>/hooks" --jq '.[] | select(.config.url | endswith("/hooks/<INSTANCE>")) | .id')

# 2. the failed deliveries of the last day (any status other than OK), one id per line
SINCE=$(date -u -d '1 day ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v-1d +%Y-%m-%dT%H:%M:%SZ)
FAILED=$(mktemp)
gh api --paginate "repos/<OWNER>/<REPO>/hooks/$HOOK_ID/deliveries?per_page=100" \
  --jq ".[] | select(.status != \"OK\" and .delivered_at > \"$SINCE\") | .id" > "$FAILED"

# 3. redeliver each one; GitHub sends the same X-GitHub-Delivery id, which hookspool dedups
while read -r DELIVERY_ID; do
  gh api -X POST "repos/<OWNER>/<REPO>/hooks/$HOOK_ID/deliveries/$DELIVERY_ID/attempts"
done < "$FAILED"
rm -f "$FAILED"
```

Install it as a daily cron job only after the person approves the schedule. A redelivery of a delivery that
hookspool already accepted is recognised by its dedup id and is not queued twice. `expect_every_s` is not set,
because a quiet repository is normal.

## Roll back

Delete the `[bobbins.<instance>]` table, run `hookspool check` and `hookspool reload`, then delete the
webhook in the repository's settings.
