"""Prometheus metrics exposed at /metrics.

Covers the operational signals the runbooks and alert rules depend on:
request latency, checkout failures, DB pool wait time, DB pool checked-out
connections and dependency errors.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from app.core.config import get_settings

settings = get_settings()
REGISTRY = CollectorRegistry()

# --- HTTP ---
REQUEST_COUNT = Counter(
    "checkout_api_requests_total",
    "Total HTTP requests.",
    ["method", "endpoint", "status"],
    registry=REGISTRY,
)
REQUEST_LATENCY = Histogram(
    "checkout_api_request_duration_seconds",
    "HTTP request latency in seconds.",
    ["method", "endpoint"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    registry=REGISTRY,
)

# --- Checkout ---
CHECKOUT_LATENCY = Histogram(
    "checkout_api_checkout_duration_seconds",
    "End-to-end checkout latency in seconds.",
    buckets=(0.1, 0.25, 0.5, 1, 2, 3, 5, 10, 30),
    registry=REGISTRY,
)
CHECKOUT_ATTEMPTS = Counter(
    "checkout_api_checkout_attempts_total",
    "Checkout attempts by outcome.",
    ["status"],
    registry=REGISTRY,
)
CHECKOUT_FAILURES = Counter(
    "checkout_api_checkout_failures_total",
    "Checkout failures by error code (drives CheckoutErrorRateHigh).",
    ["error_code"],
    registry=REGISTRY,
)
CHECKOUT_IN_PROGRESS = Gauge(
    "checkout_api_checkout_in_progress",
    "Checkouts currently being processed.",
    registry=REGISTRY,
)

# --- Database pool ---
DB_POOL_WAIT = Histogram(
    "checkout_api_db_pool_wait_seconds",
    "Time spent waiting to acquire a database connection from the pool.",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    registry=REGISTRY,
)
DB_POOL_CHECKED_OUT = Gauge(
    "checkout_api_db_pool_checked_out",
    "Database connections currently checked out (drives PostgresConnectionPoolExhaustion).",
    registry=REGISTRY,
)
DB_POOL_SIZE = Gauge(
    "checkout_api_db_pool_size",
    "Configured database connection pool size.",
    registry=REGISTRY,
)
DB_POOL_OVERFLOW = Gauge(
    "checkout_api_db_pool_overflow",
    "Database connections currently in overflow.",
    registry=REGISTRY,
)
DB_POOL_EXHAUSTED_TOTAL = Counter(
    "checkout_api_db_pool_exhausted_total",
    "Times a request failed to acquire a connection before the pool timeout.",
    registry=REGISTRY,
)

# --- Dependencies ---
DEPENDENCY_LATENCY = Histogram(
    "checkout_api_dependency_duration_seconds",
    "Upstream dependency latency in seconds.",
    ["dependency", "operation"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    registry=REGISTRY,
)
DEPENDENCY_ERRORS = Counter(
    "checkout_api_dependency_errors_total",
    "Upstream dependency errors.",
    ["dependency", "error_type"],
    registry=REGISTRY,
)

# --- Cart / Redis ---
CART_OPERATIONS = Counter(
    "checkout_api_cart_operations_total",
    "Cart operations by type and outcome.",
    ["operation", "status"],
    registry=REGISTRY,
)
IDEMPOTENCY_RESULTS = Counter(
    "checkout_api_idempotency_results_total",
    "Idempotency lookups by result.",
    ["result"],
    registry=REGISTRY,
)

_METRIC_PATH_LABELS = ("cart_id", "order_id", "product_id")


def normalize_endpoint(route_path: str) -> str:
    """Collapse path parameters so latency series do not explode."""
    parts = [p for p in route_path.strip("/").split("/") if p]
    if "carts" in parts:
        return "/api/v1/carts/{cart_id}/items" if "items" in parts else "/api/v1/carts"
    if "orders" in parts:
        return "/api/v1/orders/checkout" if "checkout" in parts else "/api/v1/orders/{order_id}"
    for name in ("customers", "products", "health", "ready", "metrics"):
        if name in parts:
            return f"/{name}"
    return route_path


def record_request(method: str, path: str, status_code: int, duration: float) -> None:
    if not settings.metrics_enabled:
        return
    endpoint = normalize_endpoint(path)
    REQUEST_COUNT.labels(method=method, endpoint=endpoint, status=str(status_code)).inc()
    REQUEST_LATENCY.labels(method=method, endpoint=endpoint).observe(duration)


def observe_checkout(duration: float) -> None:
    CHECKOUT_LATENCY.observe(duration)


def record_checkout_result(success: bool, error_code: str | None = None) -> None:
    CHECKOUT_ATTEMPTS.labels(status="success" if success else "failed").inc()
    if not success and error_code:
        CHECKOUT_FAILURES.labels(error_code=error_code).inc()


def observe_db_pool_wait(seconds: float) -> None:
    DB_POOL_WAIT.observe(seconds)


def record_dependency_call(dependency: str, operation: str, seconds: float) -> None:
    DEPENDENCY_LATENCY.labels(dependency=dependency, operation=operation).observe(seconds)


def record_dependency_error(dependency: str, error_type: str) -> None:
    DEPENDENCY_ERRORS.labels(dependency=dependency, error_type=error_type).inc()


def record_idempotency(result: str) -> None:
    IDEMPOTENCY_RESULTS.labels(result=result).inc()


def record_cart_operation(operation: str, status: str = "success") -> None:
    CART_OPERATIONS.labels(operation=operation, status=status).inc()


@contextmanager
def timed_dependency(dependency: str, operation: str) -> Iterator[None]:
    """Time an upstream call and record latency/errors."""
    start = time.perf_counter()
    try:
        yield
    except Exception as exc:
        record_dependency_error(dependency, type(exc).__name__)
        raise
    finally:
        record_dependency_call(dependency, operation, time.perf_counter() - start)


def render_metrics() -> bytes:
    """Return the Prometheus text exposition payload."""
    return generate_latest(REGISTRY)
