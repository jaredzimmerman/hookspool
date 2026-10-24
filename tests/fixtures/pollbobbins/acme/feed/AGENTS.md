---
bobbin: acme.feed
provider: Acme Feed
api_docs: https://docs.github.com/en/rest
---

# Set up feed (for an agent)

## Needs from the human

- the values of `api_key` and `feed`, the key written to its file by the human

## Steps

1. **Add the table.** Goal: the dispatcher polls the feed.

   ```toml
   [bobbins.feed]
   bobbin = "acme.feed"
   secrets.api_key = { file = "/etc/hookspool/secrets/feed_api_key" }
   settings = { feed = "news" }
   outputs = { item = "hermes:feed-item" }
   ```

   Check: `hookspool check` exits 0. If not: fix what it names.
2. **Verify.** Run `hookspool status` and look at `polls.feed`. Check: `next_at` is set. If not: run `hookspool reload`.
