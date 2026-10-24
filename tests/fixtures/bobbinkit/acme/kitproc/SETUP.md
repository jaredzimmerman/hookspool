---
bobbin: acme.kitproc
provider: Acme Kitproc
webhook_docs: https://docs.github.com/en/webhooks
---

# Set up kitproc

Test fixture for the bobbin kit. Create the webhook at the provider and save its signing secret.

Names: `token`, `api_key`, `owner`.

```toml
[bobbins.kitproc]
bobbin = "acme.kitproc"
secrets.token = { file = "/etc/hookspool/secrets/kitproc_token" }
secrets.api_key = { file = "/etc/hookspool/secrets/kitproc_api_key" }
settings = { owner = "owner@example.com" }
outputs = { changed = "hermes:kitproc-changed" }
```

Verify with a test delivery, then `hookspool list --source kitproc`. Roll back by removing the table.
