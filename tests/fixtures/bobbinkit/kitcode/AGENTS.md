# Set up kitcode (for an agent)

## Needs from the human

- an account at the provider
- the values of `secret`, written to their files by the human

## Steps

1. **Add the table.** Goal: hookspool accepts deliveries.

   ```toml
   [bobbins.kitcode]
   secrets.secret = { file = "/etc/hookspool/secrets/kitcode_secret" }
   outputs = { "*" = "hermes:kitcode" }
   ```

   Check: `hookspool check` exits 0. If not: fix what it names.
2. **Register the webhook** (`outward`). Goal: the provider sends to `/hooks/kitcode`.
   Check: the provider shows it active. If not: report it.
3. **Verify.** Run `hookspool list --source kitcode`. Check: a batch is listed.
