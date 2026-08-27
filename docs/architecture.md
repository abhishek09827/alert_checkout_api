# Checkout API Architecture

## System Overview
The checkout-api is a production-grade e-commerce checkout service built with Python 3.11, FastAPI, PostgreSQL, and Redis. It handles shopping cart management, order processing, payment orchestration, and inventory coordination.

## Architecture Diagram
```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Client    │────▶│  Checkout   │────▶│  PostgreSQL │
│  (Web/App)  │     │    API      │     │  (Primary)  │
└─────────────┘     └──────┬──────┘     └─────────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │  Redis   │ │ Payment  │ │Inventory │
        │ (Cache/  │ │ Service  │ │ Service  │
        │Idempotent)│ │(External)│ │(External)│
        └──────────┘ └──────────┘ └──────────┘
```

## Core Components

### API Layer (FastAPI)
- **Routes**: `/api/v1/carts`, `/api/v1/orders`, `/health`, `/ready`, `/metrics`
- **Middleware**: Request logging, structured JSON logs, error handling
- **Validation**: Pydantic models for request/response

### Domain Layer
- **Entities**: Customer, Product, Cart, CartItem, Order, PaymentAttempt
- **Services**: CartService, CheckoutService
- **Exceptions**: Typed exceptions with error codes and retryability

### Infrastructure Layer
- **Database**: SQLAlchemy 2.0 async with QueuePool
- **Cache**: Redis for cart caching, idempotency keys, session storage
- **Metrics**: Prometheus client with custom metrics
- **Logging**: structlog with JSON output to stdout and file

### Data Flow

#### Cart Operations
1. Client POST `/api/v1/carts` with customer_id or session_id
2. CartService creates/gets active cart
3. Items added with inventory validation
4. Cart cached in Redis (TTL: 7 days)
5. Response returned with cart details

#### Checkout Flow
1. Client POST `/api/v1/orders/checkout` with cart_id, payment_method, idempotency_key
2. Idempotency check in Redis (atomic SET NX)
3. Cart validated, inventory reserved (DB transaction)
4. Order created with PENDING status
5. Payment processed via external gateway (async HTTP)
6. Order status updated based on payment result
7. Inventory confirmed or released
8. Cart marked completed
9. Idempotency response cached
10. Response returned

## Concurrency Control
- **Inventory**: Row-level locks via `SELECT FOR UPDATE` on product rows
- **Idempotency**: Redis atomic SET NX with TTL
- **Cart items**: Unique constraint on (cart_id, product_id)
- **Orders**: Unique idempotency_key constraint

## Error Handling Strategy
- **Retryable**: Dependency timeouts, transient DB errors, payment processing errors
- **Non-retryable**: Validation errors, insufficient inventory, card declined
- **Circuit Breaker**: Payment service failures trigger fast-fail mode

## Security
- **Authentication**: Header-based (X-Customer-ID, X-Session-ID) - integrate with auth service
- **Authorization**: Customer can only access own carts/orders
- **Idempotency**: Prevents duplicate charges
- **PCI**: No card data stored; tokenized via payment gateway

## Deployment Architecture
- **Container**: Multi-stage Docker build (builder + runtime)
- **Orchestration**: Kubernetes with HPA (CPU/memory based)
- **Service Mesh**: Istio for mTLS, traffic management
- **Observability**: Prometheus metrics, structured logs, distributed tracing
- **Secrets**: Kubernetes secrets for DB/Redis credentials

## Configuration
All configuration via environment variables:
- Database: `DATABASE_URL`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT`
- Redis: `REDIS_URL`, `REDIS_MAX_CONNECTIONS`
- External: `PAYMENT_SERVICE_URL`, `INVENTORY_SERVICE_URL`
- Features: `ENABLE_IDEMPOTENCY`, `ENABLE_CART_PERSISTENCE`

## Scaling Strategy
- **Horizontal**: HPA based on CPU (70%) and memory (80%)
- **Database**: Read replicas for GET operations, connection pooling
- **Redis**: Cluster mode for high availability
- **Queue**: Future - async payment processing via message queue