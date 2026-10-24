---
bobbin: acme.kittpl
provider: Acme Kittpl
webhook_docs: https://docs.github.com/en/webhooks
---

# Set up kittpl (for an agent)

## Needs from the human

- an account at the provider
- the values of `secret`, written to their files by the human

## Steps

1. **Add the table.** Goal: hookspool accepts deliveries.

   ```toml
   [bobbins.kittpl]
   bobbin = "acme.kittpl"
   secrets.secret = { file = "/etc/hookspool/secrets/kittpl_secret" }
   outputs = { "*" = "hermes:kittpl", build = "hermes:kittpl-builds" }
   ```

   Check: `hookspool check` exits 0. If not: fix what it names.
2. **Register the webhook** (`outward`). Goal: the provider sends to `/hooks/kittpl`.
   Check: the provider shows it active. If not: report it.
3. **Verify.** Run `hookspool list --source kittpl`. Check: a batch is listed.
