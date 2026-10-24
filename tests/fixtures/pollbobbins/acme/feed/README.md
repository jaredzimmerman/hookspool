---
bobbin: acme.feed
provider: Acme Feed
api_docs: https://docs.github.com/en/rest
---

# acme.feed

Fixture: a poll bobbin. The feed has no push, so the dispatcher ticks every 600 s and poller.py asks
what changed with a conditional GET.
