"""API request/response schemas."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class CustomerCreate(BaseModel):
    email: EmailStr
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    phone: str | None = Field(default=None, max_length=32)


class CustomerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    first_name: str
    last_name: str
    created_at: datetime


class ProductCreate(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    inventory_quantity: int = Field(default=0, ge=0)


class ProductResponse(BaseModel):
    id: UUID
    sku: str
    name: str
    price: Decimal
    currency: str
    inventory_quantity: int
    reserved_quantity: int
    available_quantity: int
    is_active: bool


class CartItemAdd(BaseModel):
    product_id: UUID
    quantity: int = Field(gt=0, le=999)


class CartItemUpdate(BaseModel):
    quantity: int = Field(gt=0, le=999)


class CartItemResponse(BaseModel):
    product_id: UUID
    sku: str
    name: str
    quantity: int
    unit_price: Decimal
    currency: str
    line_total: Decimal


class CartCreate(BaseModel):
    customer_id: UUID | None = None
    session_id: str | None = Field(default=None, max_length=128)


class CartResponse(BaseModel):
    id: UUID
    customer_id: UUID | None
    session_id: str | None
    status: str
    items: list[CartItemResponse]
    subtotal: Decimal
    item_count: int
    created_at: datetime
    updated_at: datetime


class CheckoutRequest(BaseModel):
    cart_id: UUID
    payment_method: str = Field(default="credit_card", min_length=3, max_length=50)


class CheckoutResponse(BaseModel):
    order_id: UUID
    order_number: str
    status: str
    payment_status: str
    total_amount: Decimal
    currency: str
    created_at: datetime
    idempotent_replay: bool = False


class PaymentAttemptResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    amount: Decimal
    currency: str
    status: str
    payment_method: str
    transaction_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    processed_at: datetime | None = None


class OrderResponse(BaseModel):
    id: UUID
    order_number: str
    customer_id: UUID | None
    cart_id: UUID | None
    status: str
    subtotal: Decimal
    tax_amount: Decimal
    shipping_amount: Decimal
    discount_amount: Decimal
    total_amount: Decimal
    currency: str
    items: list[CartItemResponse]
    payment_attempts: list[PaymentAttemptResponse]
    created_at: datetime
    updated_at: datetime
    paid_at: datetime | None = None


class HealthResponse(BaseModel):
    status: str
    service_name: str
    version: str
    environment: str
    timestamp: datetime
    checks: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: dict[str, Any]