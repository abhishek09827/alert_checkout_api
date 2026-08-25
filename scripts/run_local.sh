#!/usr/bin/env bash
# Restart checkout-api locally (dev/demo helper) and wait for liveness.
#   bash scripts/run_local.sh
set -uo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-./.venv/bin/python}"
STDOUT_LOG="${STDOUT_LOG:-/tmp/checkout-api-stdout.log}"
PID_FILE="${PID_FILE:-/tmp/checkout-api.pid}"
BASE_URL="${BASE_URL:-http://localhost:8000}"

# Stop a previous instance started by this script (or any stray uvicorn).
if [ -f "$PID_FILE" ]; then
  kill "$(cat "$PID_FILE")" 2>/dev/null || true
fi
pkill -f "[m]ain.py" >/dev/null 2>&1 || true
sleep 1

nohup "$PYTHON" main.py > "$STDOUT_LOG" 2>&1 &
echo $! > "$PID_FILE"

for _ in $(seq 1 30); do
  if curl -sf "$BASE_URL/health" >/dev/null 2>&1; then
    echo "checkout-api is up (pid $(cat "$PID_FILE"), stdout: $STDOUT_LOG)"
    exit 0
  fi
  sleep 1
done

echo "checkout-api did not become healthy; last lines of $STDOUT_LOG:"
tail -20 "$STDOUT_LOG"
exit 1
