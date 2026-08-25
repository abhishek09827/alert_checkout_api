#!/usr/bin/env bash
# Operational smoke check for checkout-api.
#
# Usage: BASE_URL=http://localhost:8000 bash scripts/smoke_check.sh
# Exits non-zero if liveness or the cart write path misbehaves.
set -uo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
LOG_FILE="${LOG_FILE:-var/log/checkout-api.log}"
failures=0

check() {
  local name="$1" expected="$2" url="$3"
  shift 3
  local code
  code=$(curl -s -o /tmp/smoke_body -w '%{http_code}' "$url" "$@")
  if [ "$code" = "$expected" ]; then
    printf 'PASS  %-28s HTTP %s\n' "$name" "$code"
  else
    printf 'FAIL  %-28s HTTP %s (expected %s)\n' "$name" "$code" "$expected"
    head -c 300 /tmp/smoke_body; echo
    failures=$((failures + 1))
  fi
}

echo "=== checkout-api smoke check against ${BASE_URL} ==="
check "liveness /health"        200 "$BASE_URL/health"
check "readiness /ready"        200 "$BASE_URL/ready"
check "metrics /metrics"        200 "$BASE_URL/metrics"
check "product catalog"         200 "$BASE_URL/api/v1/products"
check "cart create (session)"   201 "$BASE_URL/api/v1/carts" \
  -X POST -H 'Content-Type: application/json' -d '{"session_id":"smoke-check-session"}'
check "order 404 for unknown id" 404 "$BASE_URL/api/v1/orders/00000000-0000-0000-0000-000000000000"

echo
echo "=== metrics that drive alerting ==="
curl -s "$BASE_URL/metrics" | grep -E '^checkout_api_(checkout_failures_total|checkout_duration_seconds_count|db_pool_checked_out|db_pool_size|dependency_errors_total)' || true

echo
echo "=== last 5 structured log lines (${LOG_FILE}) ==="
if [ -f "$LOG_FILE" ]; then
  tail -5 "$LOG_FILE"
else
  echo "log file not found at ${LOG_FILE}"
fi

if [ "$failures" -gt 0 ]; then
  echo
  echo "SMOKE CHECK FAILED (${failures} check(s))"
  exit 1
fi

echo
echo "SMOKE CHECK PASSED"
