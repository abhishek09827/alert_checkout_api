"""PostgreSQL access: engine, pooling, session lifecycle and health checks."""
from __future__ import annotations

import time
from typing import AsyncIterator

from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError, TimeoutError as SATimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings
from app.core.errors import DatabaseUnavailableError, ErrorCode
from app.core.logging import get_logger
from app.core.metrics import (
    DB_POOL_CHECKED_OUT,
    DB_POOL_EXHAUSTED_TOTAL,
    DB_POOL_OVERFLOW,
    DB_POOL_SIZE,
    observe_db_pool_wait,
)

settings = get_settings()
logger = get_logger("checkout-api.db")


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _refresh_pool_gauges(engine: AsyncEngine) -> None:
    pool = engine.pool
    DB_POOL_SIZE.set(pool.size())
    DB_POOL_CHECKED_OUT.set(pool.checkedout())
    DB_POOL_OVERFLOW.set(pool.overflow())


def current_checked_out() -> int:
    """Best-effort read of the checked-out connection count for log context."""
    try:
        return get_engine().pool.checkedout()
    except Exception:  # pragma: no cover - diagnostics must never raise
        return -1


def get_engine() -> AsyncEngine:
    """Create (once) and return the async engine."""
    global _engine, _session_factory

    if _engine is not None:
        return _engine

    _engine = create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=settings.db_pool_pre_ping,
        echo=False,
        connect_args={
            "server_settings": {
                "application_name": f"{settings.service_name}-{settings.environment}",
                "statement_timeout": str(settings.db_statement_timeout_ms),
            },
            "command_timeout": settings.db_pool_timeout + 5,
        },
    )

    sync_pool = _engine.sync_engine.pool

    @event.listens_for(sync_pool, "checkout")
    def _on_checkout(_dbapi_conn, _record, _proxy):  # pragma: no cover - infra hook
        DB_POOL_CHECKED_OUT.inc()

    @event.listens_for(sync_pool, "checkin")
    def _on_checkin(_dbapi_conn, _record):  # pragma: no cover - infra hook
        DB_POOL_CHECKED_OUT.dec()

    logger.info(
        "database_engine_created",
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
    )
    _refresh_pool_gauges(_engine)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False, autoflush=False)
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: yields a session; handlers/services commit explicitly.

    Pool checkout timeouts and unreachable PostgreSQL are translated into
    realistic 503 responses (DATABASE_POOL_EXHAUSTED / DATABASE_UNAVAILABLE)
    instead of leaking driver exceptions as 500s.
    """
    factory = get_session_factory()
    session = factory()
    started = time.perf_counter()
    try:
        await session.connection()
        observe_db_pool_wait(time.perf_counter() - started)
        _refresh_pool_gauges(get_engine())
        yield session
    except SATimeoutError as exc:
        # SQLAlchemy raises this when no connection became free within pool_timeout.
        DB_POOL_EXHAUSTED_TOTAL.inc()
        _refresh_pool_gauges(get_engine())
        logger.error(
            "database_pool_exhausted",
            error=str(exc),
            error_type=type(exc).__name__,
            error_code="DATABASE_POOL_EXHAUSTED",
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout,
            checked_out=current_checked_out(),
            dependency="postgres",
        )
        raise DatabaseUnavailableError(
            "No database connection available before pool timeout",
            {
                "error_code": "DATABASE_POOL_EXHAUSTED",
                "pool_size": settings.db_pool_size,
                "max_overflow": settings.db_max_overflow,
                "pool_timeout_seconds": settings.db_pool_timeout,
            },
            code=ErrorCode.DATABASE_POOL_EXHAUSTED,
        ) from exc
    except (OperationalError, OSError) as exc:
        logger.error(
            "database_unavailable",
            error=str(exc),
            error_type=type(exc).__name__,
            error_code="DATABASE_UNAVAILABLE",
            dependency="postgres",
        )
        raise DatabaseUnavailableError("Database is unreachable", {"error_code": "DATABASE_UNAVAILABLE"}) from exc
    except Exception as exc:
        await session.rollback()
        # Only driver/pool failures are ours to report; anything else (e.g. a
        # request validation error) is re-raised untouched so the real handler
        # logs it instead of an misleading database error.
        if isinstance(exc, SQLAlchemyError):
            logger.error(
                "database_error",
                error=str(exc),
                error_type=type(exc).__name__,
                error_code="DATABASE_UNAVAILABLE",
                dependency="postgres",
            )
        raise
    finally:
        await session.close()
        _refresh_pool_gauges(get_engine())


async def check_database() -> dict:
    """Run a lightweight probe used by /ready."""
    engine = get_engine()
    pool = engine.pool
    started = time.perf_counter()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {
            "status": "healthy",
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "pool_size": pool.size(),
            "checked_out": pool.checkedout(),
            "overflow": pool.overflow(),
            "available": max(pool.size() - pool.checkedout(), 0)
            + max(settings.db_max_overflow - pool.overflow(), 0),
        }
    except Exception as exc:
        DB_POOL_EXHAUSTED_TOTAL.inc() if "timeout" in str(exc).lower() else None
        logger.error(
            "database_check_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            dependency="postgres",
        )
        return {
            "status": "unhealthy",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "checked_out": pool.checkedout(),
            "pool_size": pool.size(),
        }


async def create_all() -> None:
    """Create tables from metadata (used by local dev and integration tests)."""
    import app.domain.models  # noqa: F401 - ensure models are registered

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_database() -> None:
    """Dispose of the engine so no connections are left open on shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        logger.info("database_engine_closing")
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("database_engine_closed")
