#!/usr/bin/env bash
set -euo pipefail

backend_pid=""
access_pid=""

shutdown() {
  trap - TERM INT EXIT
  [[ -n "$access_pid" ]] && kill -TERM "$access_pid" 2>/dev/null || true
  [[ -n "$backend_pid" ]] && kill -TERM "$backend_pid" 2>/dev/null || true
  [[ -n "$access_pid" ]] && wait "$access_pid" 2>/dev/null || true
  [[ -n "$backend_pid" ]] && wait "$backend_pid" 2>/dev/null || true
}
trap shutdown TERM INT EXIT

python -m backend.run_server &
backend_pid=$!

# Do not expose the public API until the private backend has at least opened its
# health endpoint. MCP initialization may continue and is reflected by /api/health.
for _ in $(seq 1 60); do
  if python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3002/internal/health', timeout=1)" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$backend_pid" 2>/dev/null; then
    wait "$backend_pid"
  fi
  sleep 1
done

python -m access_layer.run_server &
access_pid=$!

# Bash wait -n returns as soon as either service exits; the EXIT trap then
# terminates its peer so the container never reports a half-alive deployment.
wait -n "$backend_pid" "$access_pid"
