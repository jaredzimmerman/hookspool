# Quick start

Run hookspool on your own machine, with a script as the destination, and watch three events become one delivery. There is no
gateway to set up and nothing to expose to the network.

You need Python 3.11 or newer and a shell (see [Install](install.md)).

```sh
git clone https://github.com/jaredzimmerman/hookspool && cd hookspool
python3 -m venv .venv && . .venv/bin/activate
pip install .

export HOOKSPOOL_HOME="$PWD/demo"
mkdir -p "$HOOKSPOOL_HOME/secrets"
printf 'demo-secret' > "$HOOKSPOOL_HOME/secrets/demo" && chmod 600 "$HOOKSPOOL_HOME/secrets/demo"

cat > "$HOOKSPOOL_HOME/show_batch.sh" <<'EOF'
#!/bin/sh
# Receives one coalesced batch on stdin; exit 0 means delivered.
cat >> "$HOOKSPOOL_STATE_DIR/batches.jsonl"
echo >> "$HOOKSPOOL_STATE_DIR/batches.jsonl"
EOF
chmod +x "$HOOKSPOOL_HOME/show_batch.sh"

cat > "$HOOKSPOOL_HOME/config.toml" <<EOF
[alerts]
dry = true

[spans]
demo = { quiet_s = 3, max_s = 10 }          # wait for a 3 s lull, at most 10 s

[sources.demo]
module = "hookspool.sources.template"       # a source defined entirely in config
scheme = "hmac-sha256-hex"
header = "X-Signature"
prefix = "sha256="
key = "project.id"                          # events with the same project coalesce
dedup = "id"
span = "demo"
action = "script:show_batch"
secrets.secret = { file = "$HOOKSPOOL_HOME/secrets/demo" }

[destinations."script:show_batch"]
argv = ["$HOOKSPOOL_HOME/show_batch.sh"]
state_dir = "$HOOKSPOOL_HOME/out"
EOF

hookspool check          # prints "ok"
hookspool migrate        # creates the spool
hookspool dispatch &     # the deliverer
hookspool intake &       # listens on 127.0.0.1:8650
```

Send three signed events, one of them a repeat, and one with a bad signature:

```sh
send() {
  body="{\"id\":\"$1\",\"project\":{\"id\":\"p1\"},\"title\":\"$2\"}"
  sig=$(printf '%s' "$body" | openssl dgst -sha256 -hmac demo-secret -hex | sed 's/^.* //')
  curl -s -X POST http://127.0.0.1:8650/hooks/demo -H "X-Signature: sha256=$sig" -d "$body"; echo
}
send e1 first      # {"status":"queued"}
send e2 second     # {"status":"queued"}
send e1 first      # {"status":"duplicate"}
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8650/hooks/demo -H 'X-Signature: sha256=00' -d '{}'   # 401

hookspool list           # one open batch with two events
sleep 8; hookspool list  # delivered: the two events arrived together, in order
cat "$HOOKSPOOL_HOME/out/batches.jsonl"
```

Both events were committed before the first reply, joined one batch, waited for the 3-second lull, and reached the script
as a single document. `hookspool show 1 --attempts` shows the delivery, `hookspool status` the health of the whole
spool. Stop the demo with `kill %1 %2`.

To see the point of the spool, stop the dispatcher, send more events, and look at `hookspool list`: the intake still
answers, the events are on disk, and they are delivered when the dispatcher starts again.

## Where to go next

* [Your first bobbin](first-bobbin.md) turns on a built-in bobbin for a real service.
* [Verifying](verifying.md) lists the checks to run before a sender points at your install.
* [Running](../operations/running.md) puts it on a host with systemd and a reverse proxy.
