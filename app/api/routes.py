"""HTTP API: health, readiness, metrics, catalog, carts and orders."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import select

from app.api.deps import (
    CartServiceDep,
    CheckoutServiceDep,
    CustomerContextDep,
    IdempotencyKeyDep,
    SessionDep,
)
from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.core.logging import get_logger
from app.core.metrics import render_metrics
from app.domain.models import Customer, Product
from app.domain.schemas import (
    CartCreate,
    CartItemAdd,
    CartItemUpdate,
    CartResponse,
    CheckoutRequest,
    CheckoutResponse,
    CustomerCreate,
    CustomerResponse,
    HealthResponse,
    OrderResponse,
    ProductCreate,
    ProductResponse,
)
from app.infrastructure.cache import check_redis
from app.infrastructure.db import check_database
from app.services.cart import CartService
from app.services.checkout import CheckoutService

settings = get_settings()
logger = get_logger("checkout-api.api")

health_router = APIRouter(tags=["health"])
catalog_router = APIRouter(prefix="/api/v1", tags=["catalog"])
cart_router = APIRouter(prefix="/api/v1/carts", tags=["carts"])
order_router = APIRouter(prefix="/api/v1/orders", tags=["orders"])


@health_router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health() -> HealthResponse:
    """Process liveness: always fast, never touches dependencies."""
    return HealthResponse(
        status="healthy",
        service_name=settings.service_name,
        version=settings.version,
        environment=settings.environment,
        timestamp=datetime.now(timezone.utc),
    )


@health_router.get("/ready", response_model=HealthResponse, summary="Readiness probe")
async def ready(response: Response) -> HealthResponse:
    """Readiness: 503 when PostgreSQL or Redis cannot be reached."""
    checks = {
        "postgres": await check_database(),
        "redis": await check_redis(),
    }
    healthy = all(check["status"] == "healthy" for check in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        logger.error("readiness_check_failed", checks=checks)
    return HealthResponse(
        status="healthy" if healthy else "unhealthy",
        service_name=settings.service_name,
        version=settings.version,
        environment=settings.environment,
        timestamp=datetime.now(timezone.utc),
        checks=checks,
    )


@health_router.get("/metrics", summary="Prometheus metrics", include_in_schema=False)
async def metrics() -> Response:
    return Response(content=render_metrics(), media_type="text/plain; version=0.0.4; charset=utf-8")


# --------------------------------------------------------------------- catalog
@catalog_router.post(
    "/customers",
    response_model=CustomerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a customer",
)
async def create_customer(payload: CustomerCreate, session: SessionDep) -> CustomerResponse:
    existing = (
        await session.execute(select(Customer).where(Customer.email == payload.email))
    ).scalar_one_or_none()
    if existing:
        return CustomerResponse.model_validate(existing)

    customer = Customer(**payload.model_dump())
    session.add(customer)
    await session.commit()
    logger.info("customer_created", customer_id=str(customer.id))
    return CustomerResponse.model_validate(customer)


@catalog_router.get("/customers/{customer_id}", response_model=CustomerResponse, summary="Get a customer")
async def get_customer(customer_id: UUID, session: SessionDep) -> CustomerResponse:
    customer = await session.get(Customer, customer_id)
    if customer is None:
        raise NotFoundError("Customer", str(customer_id))
    return CustomerResponse.model_validate(customer)


@catalog_router.get("/products", response_model=list[ProductResponse], summary="List products")
async def list_products(session: SessionDep, limit: int = 100) -> list[ProductResponse]:
    rows = (
        await session.execute(select(Product).where(Product.is_active).limit(min(limit, 500)))
    ).scalars().all()
    return [
        ProductResponse(
            id=p.id,
            sku=p.sku,
            name=p.name,
            price=p.price,
            currency=p.currency,
            inventory_quantity=p.inventory_quantity,
            reserved_quantity=p.reserved_quantity,
            available_quantity=p.available_quantity,
            is_active=p.is_active,
        )
        for p in rows
    ]


@catalog_router.post(
    "/products",
    response_model=ProductResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a product",
)
async def create_product(payload: ProductCreate, session: SessionDep) -> ProductResponse:
    existing = (
        await session.execute(select(Product).where(Product.sku == payload.sku))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": {"code": "INVENTORY_CONFLICT", "message": f"SKU {payload.sku} already exists"}},
        )
    product = Product(**payload.model_dump())
    session.add(product)
    await session.commit()
    logger.info("product_created", product_id=str(product.id), sku=product.sku)
    return ProductResponse(
        id=product.id,
        sku=product.sku,
        name=product.name,
        price=product.price,
        currency=product.currency,
        inventory_quantity=product.inventory_quantity,
        reserved_quantity=product.reserved_quantity,
        available_quantity=product.available_quantity,
        is_active=product.is_active,
    )


# ----------------------------------------------------------------------- carts
@cart_router.post("", response_model=CartResponse, summary="Create or fetch the active cart")
async def upsert_cart(
    payload: CartCreate,
    cart_service: CartServiceDep,
    customer_id: CustomerContextDep,
    response: Response,
) -> CartResponse:
    """Creates a cart or returns the existing active one (idempotent by design)."""
    resolved_customer = payload.customer_id
    if resolved_customer is None and customer_id:
        try:
            resolved_customer = UUID(customer_id)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": {"code": "VALIDATION_ERROR", "message": "X-Customer-ID is not a UUID"}},
            ) from None

    cart, created = await cart_service.get_or_create(
        customer_id=resolved_customer, session_id=payload.session_id
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return CartService.to_response(cart)


@cart_router.get("/{cart_id}", response_model=CartResponse, summary="Get a cart")
async def get_cart(cart_id: UUID, cart_service: CartServiceDep) -> CartResponse:
    return CartService.to_response(await cart_service.get(cart_id))


@cart_router.post("/{cart_id}/items", response_model=CartResponse, summary="Add an item to a cart")
async def add_cart_item(
    cart_id: UUID, payload: CartItemAdd, cart_service: CartServiceDep
) -> CartResponse:
    return CartService.to_response(await cart_service.add_item(cart_id, payload))


@cart_router.put(
    "/{cart_id}/items/{product_id}", response_model=CartResponse, summary="Set item quantity"
)
async def update_cart_item(
    cart_id: UUID, product_id: UUID, payload: CartItemUpdate, cart_service: CartServiceDep
) -> CartResponse:
    return CartService.to_response(await cart_service.update_item(cart_id, product_id, payload.quantity))


@cart_router.delete(
    "/{cart_id}/items/{product_id}", response_model=CartResponse, summary="Remove item from cart"
)
async def remove_cart_item(cart_id: UUID, product_id: UUID, cart_service: CartServiceDep) -> CartResponse:
    return CartService.to_response(await cart_service.remove_item(cart_id, product_id))


@cart_router.delete("/{cart_id}/items", response_model=CartResponse, summary="Empty a cart")
async def clear_cart(cart_id: UUID, cart_service: CartServiceDep) -> CartResponse:
    return CartService.to_response(await cart_service.clear(cart_id))


# ---------------------------------------------------------------------- orders
@order_router.post(
    "/checkout",
    response_model=CheckoutResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Checkout a cart (idempotent)",
)
async def checkout(
    payload: CheckoutRequest,
    checkout_service: CheckoutServiceDep,
    idempotency_key: IdempotencyKeyDep,
    response: Response,
) -> CheckoutResponse:
    """Reserves stock, charges the payment gateway and creates an order.

    Send an `Idempotency-Key` header to make retries safe: replays return the
    original order with HTTP 200 and `idempotent_replay=true`.
    """
    result = await checkout_service.process_checkout(payload, idempotency_key)
    if result.idempotent_replay:
        response.status_code = status.HTTP_200_OK
    return result


@order_router.get("/{order_id}", response_model=OrderResponse, summary="Get order state")
async def get_order(order_id: UUID, checkout_service: CheckoutServiceDep) -> OrderResponse:
    order = await checkout_service.get_order(order_id)
    return CheckoutService.to_order_response(order)


def register_routers(app) -> None:
    """Attach every router to the application."""
    app.include_router(health_router)
    app.include_router(catalog_router)
    app.include_router(cart_router)
    app.include_router(order_router)