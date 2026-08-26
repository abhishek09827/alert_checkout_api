"""Unit tests for checkout total calculation and idempotency semantics.

These tests use lightweight fakes so they run without PostgreSQL or Redis.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from app.core.errors import ErrorCode, IdempotencyConflictError, NotFoundError, PaymentError, ValidationError
from app.services.checkout import (
    FREE_SHIPPING_THRESHOLD,
    SHIPPING_FLAT,
    TAX_RATE,
    CheckoutService,
)


class FakeItem:
    def __init__(self, quantity: int, unit_price: Decimal):
        self.quantity = quantity
        self.unit_price = unit_price
        self.product_id = uuid4()

    @property
    def line_total(self) -> Decimal:
        return self.unit_price * self.quantity


class FakeCart:
    """Minimal stand-in exposing the attributes the service reads."""

    def __init__(self, items: list[FakeItem]):
        self.id = uuid4()
        self.items = items

    @property
    def subtotal(self) -> Decimal:
        return sum((item.line_total for item in self.items), Decimal("0.00"))


@pytest.fixture()
def service() -> CheckoutService:
    # session is unused by _calculate_totals, so None keeps the test honest
    return CheckoutService(session=None)  # type: ignore[arg-type]


def test_totals_add_tax_and_flat_shipping(service: CheckoutService) -> None:
    cart = FakeCart([FakeItem(quantity=2, unit_price=Decimal("10.00"))])

    totals = service._calculate_totals(cart)  # noqa: SLF001 - unit under test

    assert totals["subtotal"] == Decimal("20.00")
    assert totals["tax"] == (Decimal("20.00") * TAX_RATE).quantize(Decimal("0.01"))
    assert totals["shipping"] == SHIPPING_FLAT
    assert totals["total"] == totals["subtotal"] + totals["tax"] + SHIPPING_FLAT


def test_shipping_is_free_above_threshold(service: CheckoutService) -> None:
    cart = FakeCart([FakeItem(quantity=1, unit_price=FREE_SHIPPING_THRESHOLD)])

    totals = service._calculate_totals(cart)  # noqa: SLF001

    assert totals["shipping"] == Decimal("0.00")


def test_multiple_items_are_summed(service: CheckoutService) -> None:
    cart = FakeCart(
        [
            FakeItem(quantity=3, unit_price=Decimal("19.99")),
            FakeItem(quantity=1, unit_price=Decimal("5.01")),
        ]
    )

    totals = service._calculate_totals(cart)  # noqa: SLF001

    assert totals["subtotal"] == Decimal("64.98")


def test_error_codes_and_status_codes_are_stable() -> None:
    assert ValidationError("bad input").status_code == 400
    assert NotFoundError("Cart", "abc").code is ErrorCode.CART_NOT_FOUND
    assert NotFoundError("Order", "abc").code is ErrorCode.ORDER_NOT_FOUND
    assert IdempotencyConflictError("key-1").status_code == 409
    assert PaymentError("declined").status_code == 402


def test_error_payload_shape() -> None:
    payload = NotFoundError("Product", "42").to_payload()

    assert payload["error"]["code"] == "PRODUCT_NOT_FOUND"
    assert payload["error"]["retryable"] is False
    assert payload["error"]["details"]["resource"] == "Product"
    assert payload["error"]["service_name"] == "checkout-api"
