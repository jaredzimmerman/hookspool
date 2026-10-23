#!/bin/sh
# Write unified diffs of the ported delta scripts against the production originals into deploy/local/scripts/
# (gitignored: the originals carry account names and paths, so the diffs must not be committed).
# usage: deploy/scripts/make_diffs.sh /path/to/vps-src/scripts
set -eu
src=${1:?usage: make_diffs.sh <dir holding the original gmail_delta.py and gcal_delta.py>}
here=$(cd "$(dirname "$0")" && pwd)
out="$here/../local/scripts"
mkdir -p "$out"
for name in gmail_delta gcal_delta; do
  diff -u "$src/$name.py" "$here/$name.py" > "$out/$name.diff" || true
  printf '%s: %s lines\n' "$out/$name.diff" "$(wc -l < "$out/$name.diff" | tr -d ' ')"
done
