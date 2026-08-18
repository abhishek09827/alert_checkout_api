"""Error taxonomy and HTTP exception handlers for checkout-api.

Every failure the business can produce maps to a stable error code so that
alerts, logs, runbooks and dashboards all speak the same language.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.logging import get_logger

logger = get_logger("checkout-api.errors")


class ErrorCode(str, Enum):
    # 400 validation
    VALIDATION_ERROR = "VALIDATION_ERROR"
    CART_EMPTY = "CART_EMPTY"
    INVALID_CUSTOMER = "INVALID_CUSTOMER"
    INVALID_PAYMENT_METHOD = "INVALID_PAYMENT_METHOD"

    # 404
    CART_NOT_FOUND = "CART_NOT_FOUND"
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"
    CUSTOMER_NOT_FOUND = "CUSTOMER_NOT_FOUND"

    # 409 conflict
    INVENTORY_CONFLICT = "INVENTORY_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    CART_NOT_ACTIVE = "CART_NOT_ACTIVE"

    # 402 payment
    PAYMENT_REJECTED = "PAYMENT_REJECTED"
    PAYMENT_TIMEOUT = "PAYMENT_TIMEOUT"

    # 503 dependencies
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    DATABASE_POOL_EXHAUSTED = "DATABASE_POOL_EXHAUSTED"
    REDIS_UNAVAILABLE = "REDIS_UNAVAILABLE"
    DEPENDENCY_TIMEOUT = "DEPENDENCY_TIMEOUT"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"

    # 500
    INTERNAL_ERROR = "INTERNAL_ERROR"


class AppError(Exception):
    """Base class for all business errors."""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    status_code: int = 500
    retryable: bool = False

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "retryable": self.retryable,
                "details": self.details,
                "service_name": "checkout-api",
            }
        }


class ValidationError(AppError):
    code = ErrorCode.VALIDATION_ERROR
    status_code = 400


class NotFoundError(AppError):
    status_code = 404
    code = ErrorCode.ORDER_NOT_FOUND

    def __init__(self, resource: str, identifier: str):
        code = {
            "cart": ErrorCode.CART_NOT_FOUND,
            "order": ErrorCode.ORDER_NOT_FOUND,
            "product": ErrorCode.PRODUCT_NOT_FOUND,
            "customer": ErrorCode.CUSTOMER_NOT_FOUND,
        }.get(resource.lower(), ErrorCode.INTERNAL_ERROR)
        super().__init__(f"{resource} not found", {"resource": resource, "id": identifier})
        self.code = code
        self.status_code = 404


class ConflictError(AppError):
    code = ErrorCode.INVENTORY_CONFLICT
    status_code = 409
    retryable = True


class InventoryError(ConflictError):
    code = ErrorCode.INVENTORY_CONFLICT

    def __init__(self, product_id: str, requested: int, available: int):
        super().__init__(
            "Insufficient inventory",
            {"product_id": product_id, "requested": requested, "available": available},
        )
        self.status_code = 409


class IdempotencyConflictError(AppError):
    code = ErrorCode.IDEMPOTENCY_CONFLICT
    status_code = 409

    def __init__(self, key: str):
        super().__init__("Request with this idempotency key is already being processed", {"idempotency_key": key})


class PaymentError(AppError):
    code = ErrorCode.PAYMENT_REJECTED
    status_code = 402

    def __init__(self, message: str, code: ErrorCode | None = None, details: dict[str, Any] | None = None, retryable: bool = False):
        super().__init__(message, details)
        if code:
            self.code = code
        self.retryable = retryable


class DatabaseUnavailableError(AppError):
    code = ErrorCode.DATABASE_UNAVAILABLE
    status_code = 503
    retryable = True

    def __init__(
        self,
        message: str = "Database unavailable",
        details: dict[str, Any] | None = None,
        code: ErrorCode | None = None,
    ):
        super().__init__(message, details)
        if code is not None:
            self.code = code


class DependencyUnavailableError(AppError):
    code = ErrorCode.DEPENDENCY_UNAVAILABLE
    status_code = 503
    retryable = True

    def __init__(self, service: str, message: str = "Upstream dependency unavailable", details: dict[str, Any] | None = None):
        super().__init__(message, {**(details or {}), "dependency": service})


def register_exception_handlers(app: FastAPI) -> None:
    """Attach JSON error handlers that also emit structured logs."""

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        log = logger.warning if exc.status_code < 500 else logger.error
        log(
            "request_failed",
            error_code=exc.code.value,
            error_message=exc.message,
            status_code=exc.status_code,
            error_details=exc.details,
            retryable=exc.retryable,
        )
        return JSONResponse(status_code=exc.status_code, content=exc.to_payload())

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unhandled_exception",
            error=str(exc),
            error_type=type(exc).__name__,
            route=request.url.path,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": ErrorCode.INTERNAL_ERROR.value,
                    "message": "Internal server error",
                    "retryable": False,
                    "details": {},
                    "service_name": "checkout-api",
                }
            },
        )
