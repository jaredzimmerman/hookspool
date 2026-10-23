# GitHub webhook: agent setup

Same procedure as SETUP.md, as steps. Read the **Needs from the human** list first. Repeat steps 1 to 6
for each instance (`github-ci`, `github-prs`).

## Needs from the human

- The repository (`owner/name`) and the events to send.
- Which instance name to use, and the public HTTPS callback URL.
- A webhook secret the person generates and provides. Write it to the file in step 1; do not print it.
- Approval before the webhook is created on GitHub (step 3 is outward).

## Steps

1. **Goal:** the secret file exists. Write the secret to `/etc/hookspool/secrets/<INSTANCE>` (mode 0600).
   **Check:** `ls -l /etc/hookspool/secrets/<INSTANCE>` shows mode `-rw-------`.
2. **Goal:** the instance table is in the config. Add:
   ```toml
   [bobbins.<INSTANCE>]
   bobbin = "github"
   secrets.secret = { file = "/etc/hookspool/secrets/<INSTANCE>" }
   outputs = { "*" = "hermes:<INSTANCE>" }
   ```
   **Check:** `hookspool bobbin expand <INSTANCE>` succeeds and shows the secret as a reference only.
   Then `hookspool check` shows no error that mentions `<INSTANCE>`.
3. **Outward:** create the webhook at `https://github.com/<OWNER>/<REPO>/settings/hooks` (or with
   `POST /repos/<OWNER>/<REPO>/hooks`) with `config.url=<CALLBACK_URL>`, `config.content_type=json`,
   `config.secret=<SECRET>`, and the events chosen. Ask before running this.
4. **Goal:** the `ping` is accepted. Redeliver the ping from the webhook's recent deliveries.
   **Check:** the delivery has status 200 and `hookspool list --source <INSTANCE>` shows no event for it.
   **If it fails:** a 401 means the secret differs; compare the file with the webhook's secret.
5. **Goal:** a real delivery is queued. Ask the person to push a commit, then run
   `hookspool list --source <INSTANCE>`.
   **Check:** one queued event for the push. **If it fails:** an `unverified` result means the body was
   changed or the secret differs.
6. **Goal:** the sweep is in place. GitHub does not retry, so a daily job must redeliver failed deliveries.
   Set it up as the operator's cron (ask before installing it).
7. **Rollback:** remove `[bobbins.<INSTANCE>]`, run `hookspool check` and `hookspool reload`, and delete the
   webhook (outward, ask first).
