# Postgres Connection Pool Exhaustion Runbook

## Overview
This runbook covers the diagnosis and mitigation of PostgreSQL connection pool exhaustion in the checkout-api service.

## Symptoms
- Increased request latency (p99 > 2s)
- HTTP 503 responses with error code `DATABASE_POOL_EXHAUSTED`
- Log entries showing `db_connection_checked_out` at pool capacity
- Prometheus alert: `PostgresConnectionPoolExhaustion`
- Database connection wait time histogram (`checkout_api_db_pool_wait_seconds`) showing elevated values

## Verification Commands
```bash
# Check current pool status via metrics
curl -s http://localhost:8000/metrics | grep checkout_api_db_pool

# Check database health endpoint
curl -s http://localhost:8000/ready | jq .checks.database

# Check recent logs for pool exhaustion
grep "DATABASE_POOL_EXHAUSTED" /var/log/checkout-api.log | tail -20

# Check PostgreSQL connection count
kubectl exec -it postgres-0 -- psql -U checkout -d checkout -c "
SELECT count(*) as total_connections, 
       count(*) filter (where state = 'active') as active,
       count(*) filter (where state = 'idle') as idle
FROM pg_stat_activity WHERE datname = 'checkout';
"
```

## Root Cause Analysis
The checkout-api uses SQLAlchemy's QueuePool with configurable `pool_size` and `max_overflow`. Pool exhaustion occurs when:

1. **Concurrent checkout load exceeds pool capacity** - Each checkout request holds a DB connection for the duration of the transaction
2. **Long-running transactions** - Payment processing with external services can hold connections for 5-10 seconds
3. **Connection leaks** - Unhandled exceptions preventing connection return to pool
4. **Misconfigured pool settings** - pool_size too low for traffic patterns

Recent deployment `increase-db-pool-for-black-friday` (commit `a1b2c3d`) increased `pool_size` from 10 to 20 but reduced `max_overflow` from 10 to 5, which under burst load can cause faster exhaustion.

## Mitigation Steps

### Immediate (within 5 minutes)
1. **Scale horizontally** - Increase pod replicas to distribute load
   ```bash
   kubectl scale deployment checkout-api --replicas=10 -n checkout
   ```

2. **Increase pool overflow temporarily** - Patch deployment with higher max_overflow
   ```bash
   kubectl set env deployment/checkout-api DB_MAX_OVERFLOW=20 -n checkout
   ```

3. **Enable connection pooling at PG level** - Use PgBouncer in transaction mode
   ```bash
   kubectl apply -f deploy/pgbouncer.yaml
   ```

### Short-term (within 30 minutes)
1. **Optimize checkout transaction duration** - Move payment calls outside DB transaction
2. **Add circuit breaker** - Fail fast when pool > 80% utilized
3. **Implement request queuing** - Add application-level semaphore

### Long-term
1. **Migrate to async connection pooling** - Use asyncpg native pooling
2. **Implement read replicas** - Offload read queries
3. **Add connection pool monitoring dashboard**

## Rollback Criteria
Roll back the `increase-db-pool-for-black-friday` deployment if:
- Pool exhaustion alerts persist after horizontal scaling
- Error rate > 5% for 10 consecutive minutes
- P99 latency > 5s sustained

```bash
# Rollback deployment
kubectl rollout undo deployment/checkout-api -n checkout
# Record rollback
python scripts/deployment_record.py --environment production --version 0.1.0 --commit-sha PREVIOUS_SHA --status rolled_back --deployed-by ops-team
```

## Monitoring
- **Primary**: `checkout_api_db_pool_checked_out` gauge
- **Secondary**: `checkout_api_db_pool_wait_seconds` histogram (p99)
- **Alert**: `PostgresConnectionPoolExhaustion` when checked_out >= pool_size + max_overflow - 2 for 2m

## Related Runbooks
- [Checkout High Latency](checkout-latency.md)
- [Database Migration Procedures](../operations.md#database-migrations)