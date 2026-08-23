"""Application factory, middleware, lifespan and graceful shutdown."""
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import register_routers
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import (
    bind_request_context,
    clear_request_context,
    get_logger,
    new_request_id,
    setup_logging,
)
from app.core.metrics import record_request
from app.infrastructure.cache import close_redis
from app.infrastructure.db import close_database, get_engine

settings = get_settings()
logger = get_logger("checkout-api.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown: warm the pool, then release every connection."""
    setup_logging()
    logger.info(
        "service_starting",
        version=settings.version,
        db_pool_size=settings.db_pool_size,
        db_max_overflow=settings.db_max_overflow,
        db_pool_timeout=settings.db_pool_timeout,
    )
    get_engine()  # fail fast if the database URL is unusable
    try:
        yield
    finally:
        logger.info("service_stopping", reason="shutdown_signal")
        await close_database()
        await close_redis()
        logger.info("service_stopped")


def create_app() -> FastAPI:
    """Build the FastAPI application."""
    setup_logging()

    app = FastAPI(
        title="checkout-api",
        description="Production-style e-commerce checkout API (cart, order, payment).",
        version=settings.version,
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def observability_middleware(request: Request, call_next):
        """Bind log context, time the request and emit one access log per call."""
        request_id = request.headers.get("X-Request-ID") or new_request_id()
        bind_request_context(request_id, request.url.path, request.method)
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            duration = time.perf_counter() - started
            record_request(request.method, request.url.path, 500, duration)
            logger.error(
                "request_failed",
                request_id=request_id,
                duration_ms=round(duration * 1000, 2),
                error_code="INTERNAL_ERROR",
            )
            clear_request_context()
            raise

        duration = time.perf_counter() - started
        response.headers["X-Request-ID"] = request_id
        record_request(request.method, request.url.path, response.status_code, duration)
        logger.info(
            "request_completed",
            request_id=request_id,
            status_code=response.status_code,
            duration_ms=round(duration * 1000, 2),
        )
        clear_request_context()
        return response

    register_exception_handlers(app)
    register_routers(app)
    return app


app = create_app()
