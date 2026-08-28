# Checkout High Latency Runbook

## Overview
This runbook addresses elevated checkout latency (p99 > 3s) in the checkout-api service.

## Symptoms
- Prometheus alert: `CheckoutHighLatency` (checkout_api_checkout_duration_seconds p99 > 3s)
- Customer complaints about slow checkout
- Increased payment timeout errors
- Elevated `checkout_api_request_duration_seconds` for `/api/v1/orders/checkout`

## Verification Commands
```bash
# Check checkout latency metrics
curl -s http://localhost:8000/metrics | grep checkout_api_checkout_duration

# Check dependency latencies
curl -s http://localhost:8000/metrics | grep checkout_api_dependency_duration

# Check recent slow checkouts in logs
grep "checkout_completed" /var/log/checkout-api.log | awk '{print $NF}' | sort -n | tail -20

# Check payment service latency
curl -s http://localhost:8000/metrics | grep 'payment_gateway.*charge'
```

## Root Cause Analysis
Checkout latency is composed of:
1. **Database operations** (inventory check, reservation, order creation) - typically 50-200ms
2. **Payment gateway call** - typically 100-500ms, can exceed 5s under load
3. **Inventory service call** - typically 50-200ms
4. **Application overhead** - serialization, validation - typically <50ms

Common causes:
- **Payment gateway degradation** - External service slow/unavailable
- **Database pool contention** - Waiting for connections (see postgres-connection-pool.md)
- **Lock contention** - Row-level locks on popular products
- **Network issues** - DNS, latency to external services
- **Garbage collection pauses** - Python GC during high allocation

## Mitigation Steps

### Immediate
1. **Check payment service health**
   ```bash
   curl -s http://payment-service:8001/health
   # If degraded, enable circuit breaker
   kubectl set env deployment/checkout-api PAYMENT_CIRCUIT_BREAKER=true -n checkout
   ```

2. **Scale checkout-api pods**
   ```bash
   kubectl scale deployment checkout-api --replicas=15 -n checkout
   ```

3. **Increase payment timeout** (temporary)
   ```bash
   kubectl set env deployment/checkout-api PAYMENT_SERVICE_TIMEOUT=10 -n checkout
   ```

### Short-term
1. **Implement async payment processing** - Return order immediately, process payment async
2. **Add payment fallback** - Secondary payment provider
3. **Cache inventory data** - Reduce DB queries for availability checks

### Long-term
1. **Event-driven checkout** - Use message queue for payment processing
2. **Implement saga pattern** - Compensating transactions for failures
3. **Add distributed tracing** - OpenTelemetry for end-to-end visibility

## Rollback Criteria
Roll back if:
- Latency persists after scaling and circuit breaker
- Error rate > 10% for 5 minutes
- Payment success rate drops below 90%

```bash
kubectl rollout undo deployment/checkout-api -n checkout
python scripts/deployment_record.py --environment production --version 0.1.0 --commit-sha PREVIOUS_SHA --status rolled_back --deployed-by ops-team
```

## Monitoring
- **Primary**: `checkout_api_checkout_duration_seconds` (p50, p95, p99)
- **Breakdown**: `checkout_api_dependency_duration_seconds` by service/operation
- **Alert**: `CheckoutHighLatency` when p99 > 3s for 5m

## Related Runbooks
- [Postgres Connection Pool](postgres-connection-pool.md)
- [Checkout Error Rate](checkout-error-rate.md)