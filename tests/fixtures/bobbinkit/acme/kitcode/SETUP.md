---
bobbin: acme.kitcode
provider: Acme Kitcode
webhook_docs: https://docs.github.com/en/webhooks
---

# Set up kitcode

Test fixture for the bobbin kit. Create the webhook at the provider and save its signing secret.

Names: `secret`.

```toml
[bobbins.kitcode]
bobbin = "acme.kitcode"
secrets.secret = { file = "/etc/hookspool/secrets/kitcode_secret" }
outputs = { "*" = "hermes:kitcode" }
```

Verify with a test delivery, then `hookspool list --source kitcode`. Roll back by removing the table.
