# Drills

Rehearse a failure before you rely on the system. The gateway-down drill is the one that matters most.

## Gateway-down drill

Run it after the install step of [Running](running.md#rollout-order-and-rollback-one-source-at-a-time), and again before you rely on it. It proves the three claims that matter: the intake keeps
answering while the destination is down, nothing grows without bound, and everything is delivered, in order, after.
It does not restart or stop the gateway: the destination is made unreachable from hookspool's side, which the
dispatcher cannot tell from a real outage.

```
export HOOKSPOOL_HOME=/var/lib/hookspool
# (set `dry = true` under [alerts] first if you do not want the chat woken; the alert is then only recorded)

# 1. baseline
hookspool status                                   # breaker closed for hermes:*, nothing pending

# 2. take the destination away: point hermes.base_url at a closed port, then reload
sed -i 's#^base_url = .*#base_url = "http://127.0.0.1:9"#' $HOOKSPOOL_HOME/config.toml
hookspool reload

# 3. feed it. Use real traffic, or inject into the dedicated drill route (same validation as the intake; one
#    batch per key, so several keys). hermes:drill needs a Hermes subscription named "drill" that does nothing
#    visible (see below); never inject drill payloads into a real route.
for i in 1 2 3 4 5 6; do
  echo "{\"drill\": $i}" | hookspool enqueue --source drill --key drill-$i --destination hermes:drill --dedup-id drill-$i
done

# 4. watch (give it two minutes: the breaker trips on the third consecutive transport failure)
hookspool status       # hermes:drill breaker: open after 3 transport failures; oldest_pending_age_s grows
hookspool list         # batches sealed or in retry, none dead; no batch exceeds [coalesce] max_events / max_bytes
hookspool issues       # exactly one delivery issue for hermes:drill, one alert (dry: recorded, not sent)
curl -s http://127.0.0.1:8650/healthz   # 200, degraded lists the open breaker. The intake is still taking events.

# 5. restore
sed -i 's#^base_url = .*#base_url = "http://127.0.0.1:8644"#' $HOOKSPOOL_HOME/config.toml
hookspool reload

# 6. confirm recovery (the next probe is within the breaker cooldown, at most 10 minutes)
hookspool status       # breaker closed; the backlog drains FIFO per key
hookspool list --state delivered
hookspool issues       # the issue auto-resolved
```

Pass criteria: nothing `dead`; every injected event appears in a `delivered` batch; the issue opened once and
resolved itself; no batch grew past its bounds while the breaker was open. To drill with the real gateway, stop it
for a few minutes instead of step 2; that is the same test with the real thing, and the one to run before trusting
an upgrade window.

**The drill route.** Add a `drill` entry to the Hermes subscriptions file (the one `hermes.subscriptions_path`
names) with a prompt that does nothing visible and `deliver` set to `log` (so no chat is messaged), a secret of its
own, and no `events` filter. The restore step delivers the six `{"drill": n}` payloads to it, and only to it: no real
agent is woken with drill data. Real traffic that arrives while the destination is away is held the same way and is
delivered after step 5, in order; that is the point of the drill, not a loss. If the drill leaves rows behind,
they are test rows: `hookspool drop <batch>… --yes` for any still pending.
