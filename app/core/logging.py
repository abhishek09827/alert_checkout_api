"""Structured JSON logging to stdout and to the OpsPilot log file.

Every record carries: timestamp, level, service_name, environment, request_id,
route, method and (when available) customer_id plus error context.
"""
from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, MutableMapping

import structlog

from app.core.config import get_settings

settings = get_settings()

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
route_var: ContextVar[str | None] = ContextVar("route", default=None)
method_var: ContextVar[str | None] = ContextVar("method", default=None)
customer_id_var: ContextVar[str | None] = ContextVar("customer_id", default=None)


def _enrich(_logger: Any, _name: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Add the fields OpsPilot relies on to every log line."""
    event_dict.setdefault("service_name", settings.service_name)
    event_dict.setdefault("environment", settings.environment)
    event_dict["timestamp"] = datetime.now(timezone.utc).isoformat()
    event_dict["level"] = event_dict.get("level", "info").upper()

    request_id = request_id_var.get()
    route = route_var.get()
    method = method_var.get()
    customer_id = customer_id_var.get()

    if request_id:
        event_dict.setdefault("request_id", request_id)
    if route:
        event_dict.setdefault("route", route)
    if method:
        event_dict.setdefault("method", method)
    if customer_id:
        event_dict.setdefault("customer_id", customer_id)
    return event_dict


def _drop_color_message(_logger: Any, _name: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    event_dict.pop("color_message", None)
    return event_dict


def setup_logging() -> None:
    """Configure structlog + stdlib logging (idempotent)."""
    log_path = Path(settings.log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, settings.log_level.upper(), logging.INFO)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        _drop_color_message,
        _enrich,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(serializer=json.dumps),
        ],
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)

    file_handler = logging.FileHandler(log_path)
    file_handler.setFormatter(formatter)

    root.addHandler(console)
    root.addHandler(file_handler)
    root.setLevel(level)

    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine", "asyncio", "asyncpg", "redis"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str = "checkout-api") -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger."""
    return structlog.get_logger(name)


def new_request_id() -> str:
    """Generate a short, log-friendly request id."""
    return uuid.uuid4().hex[:12]


def bind_request_context(request_id: str, route: str, method: str) -> None:
    """Bind request scoped values used by every log line of a request."""
    request_id_var.set(request_id)
    route_var.set(route)
    method_var.set(method)


def bind_customer(customer_id: str | None) -> None:
    if customer_id:
        customer_id_var.set(customer_id)


def clear_request_context() -> None:
    request_id_var.set(None)
    route_var.set(None)
    method_var.set(None)
    customer_id_var.set(None)
