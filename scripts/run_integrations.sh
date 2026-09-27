#!/usr/bin/env bash
# Start (or stop) the HTTP integration endpoints OpsPilot calls:
#   :8100  POST /logs/search, POST /deployments/search, POST /deployments/{id}
#   :8200  POST /search, POST /documents/{id}
#
#   bash scripts/run_integrations.sh        # start both, wait for health
#   bash scripts/run_integrations.sh stop   # stop both
set -uo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-./.venv/bin/python}"
BASE_URL="${BASE_URL:-http://127.0.0.1}"
PID_DIR="${PID_DIR:-/tmp/checkout-integrations}"

if [ "${1:-}" = "stop" ]; then
  for name in infra rag; do
    if [ -f "$PID_DIR/$name.pid" ]; then
      kill "$(cat "$PID_DIR/$name.pid")" 2>/dev/null && echo "stopped $name"
      rm -f "$PID_DIR/$name.pid"
    fi
  done
  exit 0
fi

mkdir -p "$PID_DIR"

start() {
  local name="$1" port="$2" app="$3"
  if [ -f "$PID_DIR/$name.pid" ] && kill -0 "$(cat "$PID_DIR/$name.pid")" 2>/dev/null; then
    echo "$name already running (pid $(cat "$PID_DIR/$name.pid"), port $port)"
    return
  fi
  nohup "$PYTHON" -m uvicorn "$app" --host 127.0.0.1 --port "$port" \
    > "$PID_DIR/$name.log" 2>&1 &
  echo $! > "$PID_DIR/$name.pid"
  echo "started $name (pid $(cat "$PID_DIR/$name.pid"), port $port, log $PID_DIR/$name.log)"
}

start infra 8100 app.integrations:infra_app
start rag 8200 app.integrations:rag_app

for port in 8100 8200; do
  for _ in $(seq 1 20); do
    curl -sf "$BASE_URL:$port/health" >/dev/null 2>&1 && break
    sleep 1
  done
  if curl -sf "$BASE_URL:$port/health" >/dev/null 2>&1; then
    echo "port $port healthy"
  else
    echo "port $port FAILED to start"
    exit 1
  fi
done
