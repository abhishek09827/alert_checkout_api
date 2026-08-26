"""Shared pytest fixtures.

Unit tests run with no external services. Integration tests need a reachable
PostgreSQL and Redis and are enabled with RUN_INTEGRATION=1.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("LOG_FILE", "var/log/test-checkout-api.log")
os.environ.setdefault("METRICS_ENABLED", "true")


def integration_enabled() -> bool:
    return os.getenv("RUN_INTEGRATION") == "1"


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"
