"""Application configuration loaded from environment variables."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Every value can be overridden by an env var."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Service identity (surfaced in logs, metrics, alerts, deploy records) ---
    service_name: str = "checkout-api"
    environment: str = "development"
    version: str = "0.1.0"

    # --- HTTP server ---
    host: str = "0.0.0.0"
    port: int = 8000

    # --- PostgreSQL ---
    database_url: str = "postgresql+asyncpg://checkout:checkout@localhost:5432/checkout"
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_pool_timeout: int = 30
    db_pool_recycle: int = 1800
    db_pool_pre_ping: bool = True
    db_statement_timeout_ms: int = 15000

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int = 50
    redis_timeout: float = 2.0

    # --- Behaviour ---
    idempotency_ttl_seconds: int = 86400
    cart_ttl_seconds: int = 604800
    enable_idempotency: bool = True

    # --- External dependencies (simulated unless real URLs are configured) ---
    payment_service_url: str = "http://localhost:8001"
    payment_service_timeout: float = 5.0
    payment_failure_rate: float = 0.05

    # --- Observability ---
    log_level: str = "INFO"
    log_file: str = "var/log/checkout-api.log"
    metrics_enabled: bool = True
    health_check_timeout: float = 2.0


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
