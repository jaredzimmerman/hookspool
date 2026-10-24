# GitHub webhook: agent setup

Same procedure as SETUP.md, as steps. Read the **Needs from the human** list first. Repeat steps 1 to 7
for each instance (`github-ci`, `github-prs`). The agent never receives a webhook secret: the person writes
the secret file and creates the webhook in GitHub.

## Needs from the human

- The repository (`<OWNER>/<REPO>`), the events to send, and the instance name (`<INSTANCE>`).
- The public HTTPS callback URL for the instance (`<CALLBACK_URL>`).
- The webhook secret, written by the person into `/etc/hookspool/secrets/<INSTANCE>` with mode 0600 (step 1).
  The agent does not read it, print it or repeat it.
- Approval before the webhook is created in GitHub (step 3) and before the daily sweep is installed (step 6).

## Steps

1. **Goal:** the secret file exists. The person writes the secret to `/etc/hookspool/secrets/<INSTANCE>`.
   **Check:** `test -s /etc/hookspool/secrets/<INSTANCE> && ls -l /etc/hookspool/secrets/<INSTANCE>` shows mode
   `-rw-------`. **If it fails:** ask the person to write the file again.
2. **Goal:** the instance table is in the config, loaded and valid. Add this table, with `github-ci` replaced by
   `<INSTANCE>` everywhere in it:
   ```toml
   [bobbins.github-ci]
   bobbin = "github"
   secrets.secret = { file = "/etc/hookspool/secrets/github-ci" }
   outputs = { "*" = "hermes:github-ci" }
   ```
   Then run `hookspool bobbin expand <INSTANCE>` (it must succeed and show the secret as a reference only),
   `hookspool check`, and `hookspool reload`. **Check:** no error mentions `<INSTANCE>`. **If it fails:** fix the
   table, do not change the bobbin.
3. **Outward, human-performed:** the person creates the webhook at `https://github.com/<OWNER>/<REPO>/settings/hooks`
   with the payload URL `<CALLBACK_URL>`, content type `application/json`, the secret from step 1, and the events
   chosen. The agent does not run this. Ask the person to confirm when it is done.
4. **Goal:** the `ping` is accepted. Ask the person to redeliver the ping from the webhook's recent deliveries.
   **Check:** `hookspool list --source <INSTANCE>` shows no event for it, and the delivery shows status 200.
   **If it fails:** a 401 from hookspool means the secret differs; the person compares the file with the webhook's
   secret.
5. **Goal:** a real delivery is queued. Ask the person to push a commit, then run `hookspool list --source <INSTANCE>`.
   **Check:** one queued event for the push. **If it fails:** an `unverified` result means the body was changed
   or the secret differs.
6. **Goal:** the sweep runs. GitHub does not retry, so run the sweep once now with these exact commands, on the
   operator's machine, with the GitHub CLI logged in as a user who can administer the repository:

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

   **Check:** the command finishes and `hookspool list --source <INSTANCE>` shows no new duplicate events.
   Installing it as a daily cron job is outward: ask the person first.
7. **Outward, rollback:** remove `[bobbins.<INSTANCE>]`, run `hookspool check` and `hookspool reload`, then ask the
   person to delete the webhook in GitHub.
