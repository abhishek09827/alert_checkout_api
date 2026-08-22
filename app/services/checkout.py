"""Checkout service: idempotent order creation, inventory reservation, payment."""
from __future__ import annotations

import asyncio
import random
import time
import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.errors import (
    ConflictError,
    DependencyUnavailableError,
    ErrorCode,
    IdempotencyConflictError,
    InventoryError,
    NotFoundError,
    PaymentError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.metrics import (
    CHECKOUT_IN_PROGRESS,
    observe_checkout,
    record_checkout_result,
    timed_dependency,
)
from app.domain.models import (
    Cart,
    CartItem,
    Order,
    OrderStatus,
    PaymentAttempt,
    PaymentStatus,
    Product,
)
from app.domain.schemas import (
    CartItemResponse,
    CheckoutRequest,
    CheckoutResponse,
    OrderResponse,
    PaymentAttemptResponse,
)
from app.infrastructure.cache import idempotency_begin, idempotency_store

settings = get_settings()
logger = get_logger("checkout-api.checkout")

TAX_RATE = Decimal("0.08")
SHIPPING_FLAT = Decimal("5.99")
FREE_SHIPPING_THRESHOLD = Decimal("100.00")

# Payment methods that deliberately misbehave so failures are reproducible.
DECLINING_METHODS = {"declined_card", "insufficient_funds"}
TIMEOUT_METHOD = "unstable_gateway"
VALID_METHODS = {"credit_card", "paypal", "bank_transfer", TIMEOUT_METHOD, *DECLINING_METHODS}


class CheckoutService:
    """Orchestrates cart → order → payment for a single checkout request."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------------ public
    async def process_checkout(
        self, payload: CheckoutRequest, idempotency_key: str | None = None
    ) -> CheckoutResponse:
        started = time.perf_counter()
        CHECKOUT_IN_PROGRESS.inc()
        try:
            if payload.payment_method not in VALID_METHODS:
                raise ValidationError(
                    "Unsupported payment method",
                    {"payment_method": payload.payment_method, "supported": sorted(VALID_METHODS)},
                )

            replay = await self._begin_idempotency(idempotency_key)
            if replay is not None:
                observe_checkout(time.perf_counter() - started)
                record_checkout_result(True)
                logger.info("checkout_idempotent_replay", idempotency_key=idempotency_key)
                return replay

            cart = await self._load_cart(payload.cart_id)
            await self._reserve_inventory(cart)
            totals = self._calculate_totals(cart)

            order = await self._create_order(cart, totals, idempotency_key)
            payment = await self._charge_payment(order, payload.payment_method)

            order.status = (
                OrderStatus.PAID if payment.status == PaymentStatus.CAPTURED else OrderStatus.CONFIRMED
            )
            order.paid_at = datetime.now(timezone.utc)
            cart.status = "completed"
            await self.session.commit()

            response = CheckoutResponse(
                order_id=order.id,
                order_number=order.order_number,
                status=order.status.value,
                payment_status=payment.status.value,
                total_amount=order.total_amount,
                currency=order.currency,
                created_at=order.created_at,
            )
            if idempotency_key:
                await idempotency_store(idempotency_key, response.model_dump(mode="json"))

            duration = time.perf_counter() - started
            observe_checkout(duration)
            record_checkout_result(True)
            logger.info(
                "checkout_completed",
                order_id=str(order.id),
                order_number=order.order_number,
                cart_id=str(cart.id),
                total_amount=str(order.total_amount),
                currency=order.currency,
                payment_status=payment.status.value,
                duration_ms=round(duration * 1000, 2),
            )
            return response

        except PaymentError as exc:
            await self._release_inventory(payload.cart_id)
            duration = time.perf_counter() - started
            observe_checkout(duration)
            record_checkout_result(False, exc.code.value)
            logger.warning(
                "checkout_payment_failed",
                cart_id=str(payload.cart_id),
                error_code=exc.code.value,
                error_message=exc.message,
                payment_method=payload.payment_method,
                duration_ms=round(duration * 1000, 2),
            )
            raise
        except Exception as exc:
            duration = time.perf_counter() - started
            observe_checkout(duration)
            code = getattr(exc, "code", None)
            code_value = code.value if hasattr(code, "value") else ErrorCode.INTERNAL_ERROR.value
            if not isinstance(exc, (ValidationError, ConflictError, NotFoundError, IdempotencyConflictError)):
                record_checkout_result(False, code_value)
            logger.error(
                "checkout_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                cart_id=str(payload.cart_id),
                error_code=code_value,
                duration_ms=round(duration * 1000, 2),
            )
            raise
        finally:
            CHECKOUT_IN_PROGRESS.dec()

    # ----------------------------------------------------------------- helpers
    async def _begin_idempotency(self, key: str | None) -> CheckoutResponse | None:
        if not (key and settings.enable_idempotency):
            return None
        is_new, stored = await idempotency_begin(key)
        if is_new:
            return None
        if stored is None:
            raise IdempotencyConflictError(key)
        return CheckoutResponse(**{**stored, "idempotent_replay": True})

    async def _release_inventory(self, cart_id: uuid.UUID) -> None:
        """Compensating action: give reserved stock back after a failed payment."""
        try:
            cart = await self._load_cart(cart_id)
            for item in cart.items:
                item.product.reserved_quantity = max(0, item.product.reserved_quantity - item.quantity)
            await self.session.commit()
            logger.info("inventory_released", cart_id=str(cart_id))
        except Exception as exc:  # pragma: no cover - best effort compensation
            logger.error("inventory_release_failed", cart_id=str(cart_id), error=str(exc))

    async def _load_cart(self, cart_id: uuid.UUID) -> Cart:
        result = await self.session.execute(
            select(Cart)
            .where(Cart.id == cart_id)
            .options(selectinload(Cart.items).selectinload(CartItem.product))
        )
        cart = result.scalar_one_or_none()
        if cart is None:
            raise NotFoundError("Cart", str(cart_id))
        if cart.status != "active":
            raise ConflictError("Cart is not active", {"cart_id": str(cart_id), "status": cart.status})
        if not cart.items:
            raise ValidationError("Cart is empty", {"cart_id": str(cart_id)})
        return cart

    async def _reserve_inventory(self, cart: Cart) -> None:
        """Lock product rows, verify stock and reserve it (409 on shortage)."""
        product_ids = [item.product_id for item in cart.items]
        locked = await self.session.execute(
            select(Product).where(Product.id.in_(product_ids)).with_for_update()
        )
        products = {product.id: product for product in locked.scalars().all()}

        for item in cart.items:
            product = products[item.product_id]
            if item.quantity > product.available_quantity:
                raise InventoryError(str(product.id), item.quantity, product.available_quantity)
            product.reserved_quantity += item.quantity
            item.product = product

        logger.info("inventory_reserved", cart_id=str(cart.id), item_count=len(cart.items))

    def _calculate_totals(self, cart: Cart) -> dict[str, Decimal]:
        subtotal = cart.subtotal
        tax = (subtotal * TAX_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        shipping = Decimal("0.00") if subtotal >= FREE_SHIPPING_THRESHOLD else SHIPPING_FLAT
        discount = Decimal("0.00")
        return {
            "subtotal": subtotal,
            "tax": tax,
            "shipping": shipping,
            "discount": discount,
            "total": (subtotal + tax + shipping - discount).quantize(Decimal("0.01")),
        }

    async def _create_order(
        self, cart: Cart, totals: dict[str, Decimal], idempotency_key: str | None
    ) -> Order:
        order = Order(
            order_number=f"ORD-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:8].upper()}",
            customer_id=cart.customer_id,
            cart_id=cart.id,
            status=OrderStatus.PENDING,
            subtotal=totals["subtotal"],
            tax_amount=totals["tax"],
            shipping_amount=totals["shipping"],
            discount_amount=totals["discount"],
            total_amount=totals["total"],
            currency=cart.items[0].currency if cart.items else "USD",
            idempotency_key=idempotency_key,
        )
        self.session.add(order)
        await self.session.flush()
        logger.info("order_created", order_id=str(order.id), order_number=order.order_number)
        return order

    async def _charge_payment(self, order: Order, payment_method: str) -> PaymentAttempt:
        """Call the (simulated) payment gateway and persist the attempt."""
        attempt = PaymentAttempt(
            order_id=order.id,
            amount=order.total_amount,
            currency=order.currency,
            status=PaymentStatus.PENDING,
            payment_method=payment_method,
        )
        self.session.add(attempt)
        await self.session.flush()

        try:
            with timed_dependency("payment_gateway", "charge"):
                result = await self._simulate_gateway_call(payment_method, order.total_amount)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            attempt.status = PaymentStatus.FAILED
            attempt.error_code = ErrorCode.PAYMENT_TIMEOUT.value
            attempt.error_message = "Payment gateway timed out"
            attempt.processed_at = datetime.now(timezone.utc)
            await self.session.commit()
            raise PaymentError(
                "Payment gateway timed out",
                code=ErrorCode.PAYMENT_TIMEOUT,
                details={"payment_attempt_id": str(attempt.id), "payment_method": payment_method},
                retryable=True,
            ) from exc
        except (DependencyUnavailableError, OSError) as exc:
            attempt.status = PaymentStatus.FAILED
            attempt.error_code = ErrorCode.DEPENDENCY_UNAVAILABLE.value
            attempt.error_message = str(exc)
            attempt.processed_at = datetime.now(timezone.utc)
            await self.session.commit()
            raise PaymentError(
                "Payment gateway unavailable",
                code=ErrorCode.PAYMENT_REJECTED,
                details={"payment_attempt_id": str(attempt.id)},
                retryable=True,
            ) from exc

        attempt.processed_at = datetime.now(timezone.utc)
        if result["approved"]:
            attempt.status = PaymentStatus.CAPTURED
            attempt.transaction_id = result["transaction_id"]
            logger.info(
                "payment_captured",
                order_id=str(order.id),
                payment_attempt_id=str(attempt.id),
                amount=str(order.total_amount),
                transaction_id=attempt.transaction_id,
            )
            return attempt

        attempt.status = PaymentStatus.FAILED
        attempt.error_code = result["error_code"]
        attempt.error_message = result["message"]
        logger.warning(
            "payment_rejected",
            order_id=str(order.id),
            payment_attempt_id=str(attempt.id),
            error_code=attempt.error_code,
            error_message=attempt.error_message,
        )
        raise PaymentError(
            attempt.error_message,
            code=ErrorCode.PAYMENT_REJECTED,
            details={"payment_attempt_id": str(attempt.id), "gateway_error": attempt.error_code},
            retryable=False,
        )

    async def _simulate_gateway_call(self, payment_method: str, amount: Decimal) -> dict[str, Any]:
        """Stand-in for the real PSP: latency plus realistic outcomes."""

        async def _call() -> dict[str, Any]:
            # Latency grows with basket size, mirroring real gateway behaviour.
            await asyncio.sleep(0.15 + min(float(amount) / 2000.0, 0.6))

            if payment_method == TIMEOUT_METHOD:
                if random.random() < 0.5:
                    raise TimeoutError("payment gateway timeout")
            elif payment_method in DECLINING_METHODS:
                return {
                    "approved": False,
                    "error_code": "CARD_DECLINED",
                    "message": "The card was declined by the issuer",
                }
            elif random.random() < settings.payment_failure_rate:
                return {
                    "approved": False,
                    "error_code": random.choice(["DO_NOT_HONOR", "PROCESSING_ERROR", "INVALID_CARD"]),
                    "message": "Payment was rejected by the gateway",
                }

            return {"approved": True, "transaction_id": f"txn_{uuid.uuid4().hex[:16]}"}

        return await asyncio.wait_for(_call(), timeout=settings.payment_service_timeout)

    # ------------------------------------------------------------- read models
    async def get_order(self, order_id: uuid.UUID) -> Order:
        result = await self.session.execute(
            select(Order)
            .where(Order.id == order_id)
            .options(
                selectinload(Order.payment_attempts),
                selectinload(Order.cart).selectinload(Cart.items).selectinload(CartItem.product),
            )
        )
        order = result.scalar_one_or_none()
        if order is None:
            raise NotFoundError("Order", str(order_id))
        return order

    @staticmethod
    def to_order_response(order: Order) -> OrderResponse:
        items = [
            CartItemResponse(
                product_id=item.product_id,
                sku=item.product.sku if item.product else "unknown",
                name=item.product.name if item.product else "unknown",
                quantity=item.quantity,
                unit_price=item.unit_price,
                currency=item.currency,
                line_total=item.line_total,
            )
            for item in (order.cart.items if order.cart else [])
        ]
        attempts = [
            PaymentAttemptResponse(
                id=attempt.id,
                amount=attempt.amount,
                currency=attempt.currency,
                status=attempt.status.value,
                payment_method=attempt.payment_method,
                transaction_id=attempt.transaction_id,
                error_code=attempt.error_code,
                error_message=attempt.error_message,
                created_at=attempt.created_at,
                processed_at=attempt.processed_at,
            )
            for attempt in order.payment_attempts
        ]
        return OrderResponse(
            id=order.id,
            order_number=order.order_number,
            customer_id=order.customer_id,
            cart_id=order.cart_id,
            status=order.status.value,
            subtotal=order.subtotal,
            tax_amount=order.tax_amount,
            shipping_amount=order.shipping_amount,
            discount_amount=order.discount_amount,
            total_amount=order.total_amount,
            currency=order.currency,
            items=items,
            payment_attempts=attempts,
            created_at=order.created_at,
            updated_at=order.updated_at,
            paid_at=order.paid_at,
        )