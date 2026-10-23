# GitHub webhook setup

The GitHub bobbin receives repository webhook deliveries. Each webhook has its own secret; use one
instance per webhook.

## What to create at GitHub

1. Choose the webhook's purpose and name the instance: for example `github-ci` and `github-prs`.
2. Generate a random secret, for example `openssl rand -hex 32`. Keep it private.
3. In the repository, go to Settings, Webhooks, Add webhook. Set the payload URL to the public callback,
   for example `https://hooks.example.test/hooks/github-ci`. Set content type to `application/json`,
   paste the secret, and choose the events to send. Save.
4. GitHub sends a `ping`. Confirm it shows a green tick in the webhook's recent deliveries.

## Secrets to save

- Write the secret to `/etc/hookspool/secrets/github-ci` (and `github-prs` for the second instance), mode
  0600, owned by the hookspool user.

## Config table

```toml
[bobbins.github-ci]
bobbin = "github"
secrets.secret = { file = "/etc/hookspool/secrets/github-ci" }
outputs = { "*" = "hermes:github-ci" }

[bobbins.github-prs]
bobbin = "github"
secrets.secret = { file = "/etc/hookspool/secrets/github-prs" }
outputs = { "*" = "hermes:github-prs" }
```

## Reverse proxy

Pass `/hooks/github-ci` and `/hooks/github-prs` through with the path unchanged. GitHub sends no query
string.

## Verify

1. `hookspool bobbin expand github-ci` prints the source with the secret as a reference only.
2. `hookspool check`, then `hookspool reload`.
3. In GitHub, redeliver the `ping`. It is acknowledged and not queued.
4. Push a commit to the repository. `hookspool list --source github-ci` shows a queued event.

## Sweep

GitHub does not retry a failed delivery. Run a sweep (daily, or the reason none is needed) that redelivers
any failed delivery from the last day through GitHub's API
(`GET /repos/{owner}/{repo}/hooks/{hook_id}/deliveries`, then `POST .../attempts`) and sends them through
`hookspool enqueue`. The `expect_every_s` is not set, because a quiet repository is normal.

## Roll back

Delete the `[bobbins.<instance>]` table, run `hookspool check` and `hookspool reload`, then delete the
webhook in the repository's settings.
