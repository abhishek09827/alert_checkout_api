"""FastAPI dependencies."""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import bind_customer
from app.infrastructure.db import get_session
from app.services.cart import CartService
from app.services.checkout import CheckoutService

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_cart_service(session: SessionDep) -> CartService:
    return CartService(session)


def get_checkout_service(session: SessionDep) -> CheckoutService:
    return CheckoutService(session)


async def customer_context(
    request: Request,
    x_customer_id: Annotated[str | None, Header(alias="X-Customer-ID")] = None,
) -> str | None:
    """Bind the caller's customer id to the logging context."""
    if x_customer_id:
        bind_customer(x_customer_id)
    return x_customer_id


async def idempotency_header(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str | None:
    if idempotency_key and len(idempotency_key) > 128:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"code": "VALIDATION_ERROR", "message": "Idempotency-Key too long"}},
        )
    return idempotency_key


CartServiceDep = Annotated[CartService, Depends(get_cart_service)]
CheckoutServiceDep = Annotated[CheckoutService, Depends(get_checkout_service)]
IdempotencyKeyDep = Annotated[str | None, Depends(idempotency_header)]
CustomerContextDep = Annotated[str | None, Depends(customer_context)]
