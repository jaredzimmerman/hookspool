---
bobbin: sec.edgar
provider: SEC EDGAR
api_docs: https://www.sec.gov/about/rss-feeds
---

# Set up sec.edgar (for an agent)

The same procedure as SETUP.md, as steps. EDGAR has no push, so this is a poll bobbin; read README.md for its fair-access rules.

## Needs from the human

- The contact for EDGAR's User-Agent: a name and an email address the human reads. EDGAR requires it; do not invent one.
- The CIK number of each company to follow, and the form types if not `8-K` and `4`.

This bobbin has no secret and no provider-side step.

## Steps

1. **Add the config table.** Goal: the dispatcher polls EDGAR. Add to `config.toml`:

   ```toml
   [bobbins.edgar]
   bobbin = "sec.edgar"
   settings = { user_agent = "Example Research research@example.com", ciks = ["0000000001"], forms = ["8-K", "4"] }
   outputs = { filing = "hermes:edgar" }
   ```

   Check: `hookspool check` exits 0, then `hookspool reload` exits 0. If `hookspool check` fails: fix what it names, then run both again.
2. **Verify.** Goal: ticks are scheduled and delivered. Run `hookspool status` and read `polls.edgar`, then
   `hookspool list --source edgar` after `next_at`.
   Check: `next_at` is set and a delivered batch is listed. If not: run `hookspool issues` and report it. A permanent failure
   about `user_agent` means the human must supply a contact email address; a 403 retry means EDGAR refused the request.
3. **Roll back.** Remove the `[bobbins.edgar]` table, then run `hookspool check` and `hookspool reload`.
