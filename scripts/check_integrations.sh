#!/usr/bin/env bash
# Exercise the three HTTP integrations OpsPilot calls, using the request
# shapes from docs/HTTP_INTEGRATIONS.md (OpsPilot repo).
set -uo pipefail

INFRA="${INFRA:-http://127.0.0.1:8100}"
RAG="${RAG:-http://127.0.0.1:8200}"
TODAY="${TODAY:-$(date -u +%Y-%m-%d)}"
YESTERDAY="${YESTERDAY:-$(date -u -d 'yesterday' +%Y-%m-%d 2>/dev/null || echo "$TODAY")}"
FAIL=0

check() {
  local name="$1" url="$2" payload="$3" expect="$4"
  local body code
  body=$(curl -s -w '\n%{http_code}' -X POST "$url" -H 'Content-Type: application/json' -d "$payload")
  code=$(printf '%s' "$body" | tail -1)
  printf '%s\n' "$(printf '%s' "$body" | sed '$d' | head -c 600)"
  if [ "$code" = "$expect" ]; then
    echo "  -> HTTP $code OK ($name)"
  else
    echo "  -> HTTP $code FAILED, expected $expect ($name)"
    FAIL=1
  fi
}

echo "== POST $INFRA/logs/search =="
check logs "$INFRA/logs/search" \
  "{\"service\":\"checkout-api\",\"start_time\":\"${YESTERDAY}T00:00:00+00:00\",\"end_time\":\"${TODAY}T23:59:59+00:00\",\"query\":\"checkout\",\"limit\":3}" 200

echo
echo "== POST $INFRA/deployments/search =="
check deployments "$INFRA/deployments/search" \
  '{"service":"checkout-api","start_time":"2026-08-01T00:00:00+00:00","end_time":"2026-12-31T00:00:00+00:00"}' 200

echo
echo "== POST $RAG/search =="
check rag "$RAG/search" \
  '{"query":"PostgresConnectionPoolExhaustion pool exhausted","service":"checkout-api","environment":"development","top_k":2}' 200

echo
if [ "$FAIL" -eq 0 ]; then
  echo "INTEGRATION CHECKS PASSED"
else
  echo "INTEGRATION CHECKS FAILED"
fi
exit "$FAIL"
