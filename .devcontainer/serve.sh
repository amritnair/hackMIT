#!/usr/bin/env bash
# Runs every time you attach to the codespace, including after a restart.
set -eu

# Attaching twice should not mean two servers fighting over one port.
if curl -fsS -o /dev/null --max-time 2 http://127.0.0.1:8000/; then
  echo "Prophecy is already serving on :8000"
  exit 0
fi

nohup prophecy -C /workspaces/demo serve --port 8000 \
  > /tmp/prophecy-serve.log 2>&1 &

for _ in $(seq 1 10); do
  sleep 1
  if curl -fsS -o /dev/null --max-time 2 http://127.0.0.1:8000/; then
    echo "Prophecy is serving on :8000 — open the forwarded port."
    exit 0
  fi
done

echo "Prophecy did not come up; see /tmp/prophecy-serve.log" >&2
