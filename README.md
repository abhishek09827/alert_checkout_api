# checkout-api

Operationally realistic e-commerce checkout service (FastAPI + PostgreSQL + Redis) built to be
wired into **OpsPilot.ai** over HTTP: structured JSON logs, Prometheus metrics, alert payloads,
runbooks and deployment records all share the same `service_name=checkout-api` and
`environment` labels.

```
POST /api/v1/carts          create / fetch the active cart
POST /api/v1/carts/{id}/items    add an item (inventory checked)
GET  /api/v1/orders/{id}    order state incl. payment attempts
POST /api/v1/orders/checkout      idempotent checkout
GET  /health                liveness
GET  /ready                 readiness (Postgres + Redis) -> 503 when down
GET  /metrics               Prometheus exposition
```

## Run it (from a git clone, only local Postgres + Redis needed)

```bash
git clone <repo> && cd checkout-api
docker compose up -d postgres redis     # local dependencies
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
cp .env.example .env                    # defaults point at localhost:5432 / 6379
bash scripts/run_local.sh               # starts the API and waits for /health
```

Everything in one shot (API + DB + Redis in containers):

```bash
docker compose up --build -d
curl localhost:8000/health
```

## Generate traffic

```bash
# happy path + every realistic failure (400 / 402 / 404 / 409 / timeout)
bash scripts/demo_flow.sh

# concurrent checkout load -> latency, pool wait, failure counters
./.venv/bin/python scripts/load_test.py --users 40 --requests 4
./.venv/bin/python scripts/load_test.py --users 200 --requests 10   # push the DB pool

# reproducible payment failures for alerting
#   declined_card       -> HTTP 402 PAYMENT_REJECTED
#   unstable_gateway    -> intermittent HTTP 402 PAYMENT_TIMEOUT + dependency errors
```

## Inspect logs

JSON logs go to **stdout** and to `var/log/checkout-api.log`. Every line carries
`timestamp, level, service_name, environment, request_id, route` plus error context:

```bash
bash scripts/logs.sh errors      # ERROR lines with error_code / duration
bash scripts/logs.sh checkouts   # checkout_completed / checkout_failed
bash scripts/logs.sh deps        # DB pool + dependency problems
tail -f var/log/checkout-api.log | jq .
grep DATABASE_POOL_EXHAUSTED var/log/checkout-api.log | tail
```

## Metrics and alerts

```bash
curl -s localhost:8000/metrics | grep checkout_api_
```

Key series: `checkout_api_checkout_duration_seconds`,
`checkout_api_checkout_failures_total`, `checkout_api_db_pool_wait_seconds`,
`checkout_api_db_pool_checked_out`, `checkout_api_dependency_errors_total`.

* Alert rules: `ops/alertmanager/rules.yml` (PostgresConnectionPoolExhaustion,
  CheckoutHighLatency, CheckoutErrorRateHigh, CheckoutDependencyErrors)
* Ready-to-fire webhook payloads: `ops/alert-payloads/*.json`

Send one to OpsPilot (HTTP configuration → webhook receiver):

```bash
curl -sS -X POST "$OPSPILOT_WEBHOOK_URL" \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <token>' \
  --data-binary @ops/alert-payloads/postgres-connection-pool-exhaustion.json
```

Each payload already labels `service: checkout-api` and `environment: production`, matching the
log and deployment-record fields.

## Produce a deployment record

```bash
python3 scripts/deployment_record.py \
  --environment production --version 0.1.0 \
  --commit-sha "$(git rev-parse --short HEAD)" \
  --status success --deployed-by "$(git config user.email)"
```

This appends a record with `id, timestamp, service, environment, version, commit_sha, status,
deployed_by` to `ops/deployments.json` (OpsPilot reads this path).

## Tests / CI

```bash
./.venv/bin/python -m pytest tests/unit -q     # fast, no services needed
RUN_INTEGRATION=1 ./.venv/bin/python -m pytest -q   # with Postgres + Redis up
```

`.github/workflows/ci.yml` runs formatting checks, unit tests and the container build.

## Runbooks

* `docs/runbooks/postgres-connection-pool.md` — pool exhaustion: symptoms, verification,
  mitigation, rollback criteria, monitoring
* `docs/runbooks/checkout-latency.md` — high checkout latency
* `docs/architecture.md`, `docs/operations.md`

## Run checkout-api together with OpsPilot.ai

Ports: checkout-api `8000`, evidence integrations `8100` (logs/deployments) and `8200`
(runbook RAG), OpsPilot `8008`.

```bash
# Terminal 1 - dependencies + API
cd /home/abhishek/checkout-api
docker compose up -d postgres redis
bash scripts/run_local.sh
bash scripts/run_integrations.sh          # starts :8100 (logs/deployments) and :8200 (RAG)
curl http://127.0.0.1:8000/health && curl http://127.0.0.1:8000/ready
bash scripts/check_integrations.sh        # verify the three OpsPilot contracts

# Terminal 2 - OpsPilot
cd /home/abhishek/projects/OpsPilot.ai
export GITHUB_TOKEN='your_github_pat'
TRIAGE_LLM_ENABLED=false .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8008 --reload

# Terminal 3 - register checkout-api, generate traffic, send an alert
curl -X POST http://127.0.0.1:8008/api/v2/applications -H 'Content-Type: application/json' -d '{
  "service_name": "checkout-api", "environment": "development",
  "git": {"provider":"github","repository":"abhishek09827/alert_checkout_api","auth_env_var":"GITHUB_TOKEN"},
  "logs": {"provider":"simulated_http","endpoint":"http://127.0.0.1:8100"},
  "deployments": {"provider":"simulated_http","endpoint":"http://127.0.0.1:8100"},
  "documents": {"provider":"rag_http","endpoint":"http://127.0.0.1:8200"},
  "metadata": {"baseline_commit":"93f8a27"}
}'
curl -X POST http://127.0.0.1:8008/api/v2/applications/checkout-api/development/verify

./.venv/bin/python scripts/load_test.py --users 150 --requests 10   # real traffic

curl -X POST http://127.0.0.1:8008/api/v2/alerts/triage \
  -H 'Content-Type: application/json' \
  --data-binary @ops/alert-payloads/checkout-high-latency.json
```

Evidence endpoints implemented here (contract: `docs/HTTP_INTEGRATIONS.md` in the OpsPilot repo):

| Endpoint | Backed by |
|---|---|
| `POST :8100/logs/search` | `var/log/checkout-api.log` (JSON lines, time-window + query filtered) |
| `POST :8100/deployments/search` | `ops/deployments.json` |
| `POST :8100/deployments/{id}` | single deployment record |
| `POST :8200/search` | markdown docs + runbooks, keyword retrieval with citation ids |
| `POST :8200/documents/{id}` | one document chunk by citation id |

`bash scripts/run_integrations.sh stop` shuts the evidence endpoints down.

## Incident drill

Branch `increase-db-pool-for-black-friday` changes the pool configuration in a plausible but
flawed way (larger `pool_size`, unchanged overflow). Under `scripts/load_test.py` load it
produces `DATABASE_POOL_EXHAUSTED` log lines and trips `PostgresConnectionPoolExhaustion`
— evidence lives in the logs, `git diff`, `ops/deployments.json` and the runbook, never in a
hardcoded root cause.
