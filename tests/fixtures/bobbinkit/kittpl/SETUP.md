# Set up kittpl

Test fixture for the bobbin kit. Create the webhook at the provider and save its signing secret.

Names: `secret`.

```toml
[bobbins.kittpl]
secrets.secret = { file = "/etc/hookspool/secrets/kittpl_secret" }
outputs = { "*" = "hermes:kittpl", build = "hermes:kittpl-builds" }
```

Verify with a test delivery, then `hookspool list --source kittpl`. Roll back by removing the table.
