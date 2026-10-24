---
bobbin: acme.feed
provider: Acme Feed
api_docs: https://docs.github.com/en/rest
---

# Set up feed

Test fixture for poll bobbins. Save the feed's API key `api_key` and choose the `feed`.

```toml
[bobbins.feed]
bobbin = "acme.feed"
secrets.api_key = { file = "/etc/hookspool/secrets/feed_api_key" }
settings = { feed = "news" }
outputs = { item = "hermes:feed-item" }
```

Verify: after a tick, `hookspool list --source feed` shows a delivered batch. Roll back by removing the table.
