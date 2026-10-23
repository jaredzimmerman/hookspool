# Set up kitproc (for an agent)

## Needs from the human

- an account at the provider
- the values of `token`, `api_key`, `owner`, written to their files by the human

## Steps

1. **Add the table.** Goal: hookspool accepts deliveries.

   ```toml
   [bobbins.kitproc]
   secrets.token = { file = "/etc/hookspool/secrets/kitproc_token" }
   secrets.api_key = { file = "/etc/hookspool/secrets/kitproc_api_key" }
   settings = { owner = "owner@example.com" }
   outputs = { changed = "hermes:kitproc-changed" }
   ```

   Check: `hookspool check` exits 0. If not: fix what it names.
2. **Register the webhook** (`outward`). Goal: the provider sends to `/hooks/kitproc`.
   Check: the provider shows it active. If not: report it.
3. **Verify.** Run `hookspool list --source kitproc`. Check: a batch is listed.
