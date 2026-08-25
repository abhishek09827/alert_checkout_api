#!/usr/bin/env bash
# Walks the checkout happy path plus every realistic failure mode.
# Usage: BASE_URL=http://localhost:8000 bash scripts/demo_flow.sh
set -uo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
PYTHON="${PYTHON:-python3}"
RUN_ID="$(date +%s)"

json() { "$PYTHON" -c "import json,sys; d=json.load(sys.stdin); print(d$1)"; }

echo "=== 1. liveness / readiness ==="
curl -s -o /dev/null -w 'GET /health  -> %{http_code}\n' "$BASE_URL/health"
curl -s -o /dev/null -w 'GET /ready   -> %{http_code}\n' "$BASE_URL/ready"

echo
echo "=== 2. create customer + cart ==="
CUSTOMER_ID=$(curl -s -X POST "$BASE_URL/api/v1/customers" \
  -H 'Content-Type: application/json' \
  -d '{"email":"demo@example.com","first_name":"Demo","last_name":"Shopper"}' | json "['id']")
echo "customer_id=$CUSTOMER_ID"

CART_ID=$(curl -s -X POST "$BASE_URL/api/v1/carts" \
  -H "X-Customer-ID: $CUSTOMER_ID" -H 'Content-Type: application/json' -d '{}' | json "['id']")
echo "cart_id=$CART_ID"

PRODUCT_ID=$(curl -s "$BASE_URL/api/v1/products" | "$PYTHON" -c \
  "import json,sys; p=max(json.load(sys.stdin), key=lambda x: x['available_quantity']); print(p['id'])")
LIMITED_ID=$(curl -s "$BASE_URL/api/v1/products" | "$PYTHON" -c \
  "import json,sys; print(next(x['id'] for x in json.load(sys.stdin) if x['sku']=='PROD-LIMITED'))")
echo "product_id=$PRODUCT_ID (rich stock)  limited_id=$LIMITED_ID"

echo
echo "=== 3. add item then check out (happy path) ==="
curl -s -o /dev/null -w 'POST cart item        -> %{http_code}\n' \
  -X POST "$BASE_URL/api/v1/carts/$CART_ID/items" -H 'Content-Type: application/json' \
  -d "{\"product_id\":\"$PRODUCT_ID\",\"quantity\":2}"

ORDER=$(curl -s -X POST "$BASE_URL/api/v1/orders/checkout" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: demo-$(date +%s)" \
  -d "{\"cart_id\":\"$CART_ID\",\"payment_method\":\"credit_card\"}")
echo "$ORDER" | "$PYTHON" -m json.tool
ORDER_ID=$(echo "$ORDER" | json "['order_id']")

echo
echo "=== 4. read the order back ==="
curl -s "$BASE_URL/api/v1/orders/$ORDER_ID" | "$PYTHON" -c \
  "import json,sys; d=json.load(sys.stdin); print({k: d[k] for k in ('order_number','status','total_amount')}); print('payment_attempts:', d['payment_attempts'])"

echo
echo "=== 5. failure modes ==="
echo "-- unknown order (expect 404)"
curl -s -o /dev/null -w '   -> %{http_code}\n' "$BASE_URL/api/v1/orders/00000000-0000-0000-0000-000000000000"

echo "-- validation error: empty cart checkout (expect 400)"
EMPTY_CART=$(curl -s -X POST "$BASE_URL/api/v1/carts" -H 'Content-Type: application/json' \
  -d "{\"session_id\":\"demo-empty-$RUN_ID\"}" | json "['id']")
curl -s -w '   -> %{http_code}\n' -X POST "$BASE_URL/api/v1/orders/checkout" \
  -H 'Content-Type: application/json' -d "{\"cart_id\":\"$EMPTY_CART\",\"payment_method\":\"credit_card\"}"

echo "-- inventory conflict: ask for 99 of a 3-unit SKU (expect 409)"
LIMITED_CART=$(curl -s -X POST "$BASE_URL/api/v1/carts" -H 'Content-Type: application/json' \
  -d "{\"session_id\":\"demo-limited-$RUN_ID\"}" | json "['id']")
curl -s -w '   -> %{http_code}\n' -X POST "$BASE_URL/api/v1/carts/$LIMITED_CART/items" \
  -H 'Content-Type: application/json' -d "{\"product_id\":\"$LIMITED_ID\",\"quantity\":99}"

echo "-- payment rejection using declined_card (expect 402)"
DECLINE_CART=$(curl -s -X POST "$BASE_URL/api/v1/carts" -H 'Content-Type: application/json' \
  -d "{\"session_id\":\"demo-decline-$RUN_ID\"}" | json "['id']")
curl -s -o /dev/null -X POST "$BASE_URL/api/v1/carts/$DECLINE_CART/items" \
  -H 'Content-Type: application/json' -d "{\"product_id\":\"$PRODUCT_ID\",\"quantity\":1}"
curl -s -w '   -> %{http_code}\n' -X POST "$BASE_URL/api/v1/orders/checkout" \
  -H 'Content-Type: application/json' \
  -d "{\"cart_id\":\"$DECLINE_CART\",\"payment_method\":\"declined_card\"}"

echo "-- dependency timeout using unstable_gateway (expect 402/PAYMENT_TIMEOUT)"
TIMEOUT_CART=$(curl -s -X POST "$BASE_URL/api/v1/carts" -H 'Content-Type: application/json' \
  -d "{\"session_id\":\"demo-timeout-$RUN_ID\"}" | json "['id']")
curl -s -o /dev/null -X POST "$BASE_URL/api/v1/carts/$TIMEOUT_CART/items" \
  -H 'Content-Type: application/json' -d "{\"product_id\":\"$PRODUCT_ID\",\"quantity\":1}"
for i in 1 2 3 4; do
  curl -s -w "   attempt $i -> %{http_code}\n" -o /tmp/timeout_body \
    -X POST "$BASE_URL/api/v1/orders/checkout" -H 'Content-Type: application/json' \
    -d "{\"cart_id\":\"$TIMEOUT_CART\",\"payment_method\":\"unstable_gateway\"}"
done

echo
echo "=== 6. idempotency replay (same key twice) ==="
KEY="demo-replay-$(date +%s)"
REPLAY_CART=$(curl -s -X POST "$BASE_URL/api/v1/carts" -H 'Content-Type: application/json' \
  -d "{\"session_id\":\"demo-replay-$RUN_ID\"}" | json "['id']")
curl -s -w '   add item -> %{http_code}\n' -o /tmp/replay_item \
  -X POST "$BASE_URL/api/v1/carts/$REPLAY_CART/items" \
  -H 'Content-Type: application/json' -d "{\"product_id\":\"$PRODUCT_ID\",\"quantity\":1}"
head -c 200 /tmp/replay_item; echo
curl -s -w '   first  -> %{http_code}\n' -X POST "$BASE_URL/api/v1/orders/checkout" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: $KEY" \
  -d "{\"cart_id\":\"$REPLAY_CART\",\"payment_method\":\"credit_card\"}"
curl -s -w '   replay -> %{http_code}\n' -X POST "$BASE_URL/api/v1/orders/checkout" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: $KEY" \
  -d "{\"cart_id\":\"$REPLAY_CART\",\"payment_method\":\"credit_card\"}"

echo
echo "=== 7. key metrics after the run ==="
curl -s "$BASE_URL/metrics" | grep -E '^checkout_api_(checkout_failures_total|checkout_attempts_total|db_pool_checked_out|db_pool_size|db_pool_wait_seconds_count|dependency_errors_total|idempotency_results_total)' || true
