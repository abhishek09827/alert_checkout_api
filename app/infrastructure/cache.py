"""Redis access: cart cache, idempotency keys and health checks."""
from __future__ import annotations

import json
import time
from typing import Any

import redis.asyncio as redis
from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import record_idempotency

settings = get_settings()
logger = get_logger("checkout-api.cache")

_client: Redis | None = None

CART_KEY = "cart:{customer_id}"
IDEMPOTENCY_KEY = "idempotency:{key}"
IN_PROGRESS = "__in_progress__"


async def get_redis() -> Redis:
    """Return the shared Redis client, creating it on first use."""
    global _client
    if _client is None:
        _client = redis.from_url(
            settings.redis_url,
            max_connections=settings.redis_max_connections,
            socket_timeout=settings.redis_timeout,
            socket_connect_timeout=settings.redis_timeout,
            decode_responses=True,
            health_check_interval=30,
        )
        logger.info("redis_client_created", redis_url=settings.redis_url)
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        logger.info("redis_client_closing")
        await _client.aclose()
        _client = None


async def cache_cart(customer_id: str, payload: dict[str, Any]) -> bool:
    """Best-effort cart cache write; never fails a request."""
    try:
        client = await get_redis()
        await client.setex(
            CART_KEY.format(customer_id=customer_id),
            settings.cart_ttl_seconds,
            json.dumps(payload, default=str),
        )
        return True
    except Exception as exc:
        logger.warning("cart_cache_write_failed", customer_id=customer_id, error=str(exc))
        return False


async def get_cached_cart(customer_id: str) -> dict[str, Any] | None:
    try:
        client = await get_redis()
        raw = await client.get(CART_KEY.format(customer_id=customer_id))
        return json.loads(raw) if raw else None
    except Exception as exc:
        logger.warning("cart_cache_read_failed", customer_id=customer_id, error=str(exc))
        return None


async def invalidate_cart(customer_id: str) -> None:
    try:
        client = await get_redis()
        await client.delete(CART_KEY.format(customer_id=customer_id))
    except Exception as exc:
        logger.warning("cart_cache_invalidate_failed", customer_id=customer_id, error=str(exc))


async def idempotency_begin(key: str) -> tuple[bool, dict[str, Any] | None]:
    """Atomically claim an idempotency key.

    Returns (is_new, stored_response). Redis failures fail *open* so a cache
    outage degrades performance but does not stop checkouts.
    """
    store_key = IDEMPOTENCY_KEY.format(key=key)
    try:
        client = await get_redis()
        claimed = await client.set(store_key, IN_PROGRESS, nx=True, ex=settings.idempotency_ttl_seconds)
        if claimed:
            record_idempotency("miss")
            return True, None

        stored = await client.get(store_key)
        if stored and stored != IN_PROGRESS:
            record_idempotency("hit")
            return False, json.loads(stored)

        record_idempotency("in_progress")
        return False, None
    except Exception as exc:
        record_idempotency("error")
        logger.error("idempotency_lookup_failed", idempotency_key=key, error=str(exc))
        return True, None


async def idempotency_store(key: str, response: dict[str, Any]) -> None:
    try:
        client = await get_redis()
        await client.setex(
            IDEMPOTENCY_KEY.format(key=key),
            settings.idempotency_ttl_seconds,
            json.dumps(response, default=str),
        )
    except Exception as exc:
        logger.error("idempotency_store_failed", idempotency_key=key, error=str(exc))


async def check_redis() -> dict[str, Any]:
    """Probe used by /ready."""
    started = time.perf_counter()
    try:
        client = await get_redis()
        await client.ping()
        info = await client.info("memory")
        return {
            "status": "healthy",
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "used_memory_human": info.get("used_memory_human", "unknown"),
        }
    except Exception as exc:
        logger.error(
            "redis_check_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            dependency="redis",
        )
        return {"status": "unhealthy", "error": str(exc), "error_type": type(exc).__name__}
