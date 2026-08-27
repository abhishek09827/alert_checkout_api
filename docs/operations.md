# Checkout API Operations Guide

## Table of Contents
1. [Local Development](#local-development)
2. [Deployment](#deployment)
3. [Monitoring & Alerting](#monitoring--alerting)
4. [Database Operations](#database-operations)
5. [Incident Response](#incident-response)
6. [Capacity Planning](#capacity-planning)
7. [Security Operations](#security-operations)

## Local Development

### Prerequisites
- Docker Desktop or Docker Engine + Docker Compose
- Python 3.11+ (for local development without Docker)
- Git

### Quick Start
```bash
# Clone repository
git clone <repo-url>
cd checkout-api

# Start all services
docker-compose up -d

# Verify services
curl http://localhost:8000/health
curl http://localhost:8000/ready

# Run tests
docker-compose exec checkout-api pytest tests/ -v

# View logs
docker-compose logs -f checkout-api
```

### Development Workflow
```bash
# Make changes to code
# Run formatter
ruff check . && black . && isort .

# Run tests
pytest tests/unit -v

# Build image
docker build -t checkout-api:dev .

# Run with local code mounted
docker-compose -f docker-compose.yml -f docker-compose.dev.yml up
```

## Deployment

### Docker Image
```bash
# Build
docker build -t ghcr.io/your-org/checkout-api:v0.1.0 .

# Run locally
docker run -p 8000:8000 \
  -e DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/db \
  -e REDIS_URL=redis://host:6379/0 \
  ghcr.io/your-org/checkout-api:v0.1.0
```

### Kubernetes
```bash
# Apply manifests
kubectl apply -f deploy/production.yaml

# Check rollout
kubectl rollout status deployment/checkout-api -n checkout

# Scale
kubectl scale deployment checkout-api --replicas=5 -n checkout
```

### Configuration
| Variable | Description | Default |
|----------|-------------|---------|
| `DATABASE_URL` | PostgreSQL connection string | Required |
| `REDIS_URL` | Redis connection string | Required |
| `DB_POOL_SIZE` | Connection pool size | 10 |
| `DB_MAX_OVERFLOW` | Max overflow connections | 5 |
| `DB_POOL_TIMEOUT` | Pool checkout timeout (s) | 30 |
| `LOG_LEVEL` | Logging level | INFO |
| `METRICS_ENABLED` | Enable Prometheus metrics | true |
| `PAYMENT_SERVICE_URL` | Payment service endpoint | http://localhost:8001 |
| `INVENTORY_SERVICE_URL` | Inventory service endpoint | http://localhost:8002 |
### Environment Variables
Create `.env` file:
## Monitoring & Alerting

### Key Metrics
| Metric | Description | Alert Threshold |
|--------|-------------|-----------------|
| `checkout_api_checkout_duration_seconds` | Checkout latency | p99 > 3s |
| `checkout_api_db_pool_checked_out` | Active DB connections | > pool_size + max_overflow - 2 |
| `checkout_api_checkout_failures_total` | Checkout failures by type | rate > 5% |
| `checkout_api_dependency_errors_total` | External service errors | rate > 10/min |

### Prometheus Alerts
```yaml
# PostgresConnectionPoolExhaustion
- alert: PostgresConnectionPoolExhaustion
  expr: checkout_api_db_pool_checked_out >= (checkout_api_db_pool_size + checkout_api_db_pool_overflow - 2)
  for: 2m
  labels:
    severity: critical
    service: checkout-api
  annotations:
    summary: "PostgreSQL connection pool near exhaustion"

# CheckoutHighLatency
- alert: CheckoutHighLatency
  expr: histogram_quantile(0.99, rate(checkout_api_checkout_duration_seconds_bucket[5m])) > 3
  for: 5m
  labels:
    severity: warning
    service: checkout-api
  annotations:
    summary: "Checkout p99 latency exceeds 3 seconds"

# CheckoutErrorRateHigh
- alert: CheckoutErrorRateHigh
  expr: rate(checkout_api_checkout_failures_total[5m]) > 0.05
  for: 2m
  labels:
    severity: critical
    service: checkout-api
  annotations:
    summary: "Checkout error rate exceeds 5%"
```

### Log Analysis
```bash
## Database Operations

### Migrations
```bash
# Generate migration
alembic revision --autogenerate -m "Add new column"

# Apply migrations
alembic upgrade head

# Rollback
alembic downgrade -1
```

### Connection Pool Tuning
Monitor these metrics to tune pool:
- `checkout_api_db_pool_wait_seconds` - Should be < 10ms p99
- `checkout_api_db_pool_checked_out` - Should not exceed pool_size + max_overflow

Recommended settings by traffic:
| RPS | pool_size | max_overflow |
|-----|-----------|--------------|
| < 100 | 10 | 5 |
| 100-500 | 20 | 10 |
| 500-1000 | 30 | 15 |
| > 1000 | 50 | 20 |

### Backup & Restore
```bash
# Backup
## Incident Response

### Severity Levels
- **SEV-1**: Service down, data loss, payment processing broken
- **SEV-2**: Degraded performance, high error rate, partial functionality
- **SEV-3**: Minor issues, cosmetic bugs, non-critical feature broken

### Runbooks
- [Postgres Connection Pool Exhaustion](../runbooks/postgres-connection-pool.md)
- [Checkout High Latency](../runbooks/checkout-latency.md)

### Escalation
1. On-call engineer (PagerDuty)
2. Team lead
3. Engineering manager
4. VP Engineering

### Post-Incident
1. Create incident timeline
2. Run blameless postmortem within 48 hours
3. Create action items with owners
4. Update runbooks if needed

## Capacity Planning

### Current Baseline (per pod)
- **CPU**: ~200m idle, ~800m under load
- **Memory**: ~200Mi idle, ~600Mi under load
- **DB Connections**: 10 pool + 5 overflow
- **Redis Connections**: ~20 per pod

### Scaling Triggers
- CPU > 70% for 5 minutes → Scale up
- Memory > 80% for 5 minutes → Scale up
- DB pool wait > 50ms p99 → Increase pool or scale
- Checkout p99 > 2s → Scale or investigate

### Black Friday Preparation
1. Pre-scale to 20 replicas 1 week before
2. Increase DB pool_size to 50, max_overflow to 20
3. Enable PgBouncer in transaction mode
4. Load test at 3x expected peak
5. War-room setup 24h before

## Security Operations

### Secret Rotation
```bash
# Rotate database password
kubectl create secret generic checkout-secrets \
  --from-literal=database-url="postgresql+asyncpg://user:newpass@host:5432/db" \
  --dry-run=client -o yaml | kubectl apply -f -

# Restart pods to pick up new secret
kubectl rollout restart deployment/checkout-api -n checkout
```

### Vulnerability Scanning
```bash
# Scan image
trivy image ghcr.io/your-org/checkout-api:latest

# Scan dependencies
pip-audit -r requirements.txt
```

### Audit Logging
All API requests logged with:
- Request ID
- Customer ID
- Route
- Response status
- Latency
- Error details (if any)
pg_dump -h localhost -U checkout checkout > backup_$(date +%F).sql

# Restore
psql -h localhost -U checkout checkout < backup_2024-01-15.sql
```
# Structured log queries (using jq)
# Find all errors in last hour
grep '"level":"ERROR"' /var/log/checkout-api.log | jq -r '.timestamp + " " + .error.code + " " + .message'

# Find slow checkouts
grep "checkout_completed" /var/log/checkout-api.log | jq -r 'select(.duration_ms > 3000) | .timestamp + " " + (.duration_ms|tostring) + "ms " + .order_id'

# Find payment failures
grep "payment_declined\|payment_timeout" /var/log/checkout-api.log | jq -r '.timestamp + " " + .error_code + " " + .message'
```
```env
DATABASE_URL=postgresql+asyncpg://checkout:checkout@localhost:5432/checkout
REDIS_URL=redis://localhost:6379/0
ENVIRONMENT=development
LOG_LEVEL=DEBUG
DB_POOL_SIZE=5
DB_MAX_OVERFLOW=2
```