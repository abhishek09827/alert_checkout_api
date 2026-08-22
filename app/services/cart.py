"""Cart service: cart lifecycle and item management."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import ConflictError, InventoryError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.metrics import record_cart_operation
from app.domain.models import Cart, CartItem, Customer, Product
from app.domain.schemas import CartItemAdd, CartItemResponse, CartResponse
from app.infrastructure.cache import cache_cart, invalidate_cart

logger = get_logger("checkout-api.cart")

MAX_ITEM_QUANTITY = 999


class CartService:
    """All cart reads and writes live here."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ---------------------------------------------------------------- helpers
    async def _load(self, cart_id: UUID) -> Cart:
        result = await self.session.execute(
            select(Cart)
            .where(Cart.id == cart_id)
            .options(selectinload(Cart.items).selectinload(CartItem.product))
        )
        cart = result.scalar_one_or_none()
        if cart is None:
            raise NotFoundError("Cart", str(cart_id))
        return cart

    async def _find_active(self, customer_id: UUID | None, session_id: str | None) -> Cart | None:
        stmt = select(Cart).where(Cart.status == "active")
        if customer_id:
            stmt = stmt.where(Cart.customer_id == customer_id)
        elif session_id:
            stmt = stmt.where(Cart.session_id == session_id)
        else:
            raise ValidationError("customer_id or session_id is required")
        stmt = stmt.options(selectinload(Cart.items).selectinload(CartItem.product))
        return (await self.session.execute(stmt)).scalars().first()

    async def _refresh_cache(self, cart: Cart) -> None:
        if not cart.customer_id:
            return
        await cache_cart(str(cart.customer_id), self.to_response(cart).model_dump(mode="json"))

    # ----------------------------------------------------------------- public
    async def get_or_create(
        self, customer_id: UUID | None = None, session_id: str | None = None
    ) -> tuple[Cart, bool]:
        """Return the active cart for a customer/session, creating it if needed."""
        if customer_id is not None and await self.session.get(Customer, customer_id) is None:
            raise NotFoundError("Customer", str(customer_id))

        cart = await self._find_active(customer_id, session_id)
        if cart is not None:
            record_cart_operation("get")
            return cart, False

        cart = Cart(customer_id=customer_id, session_id=session_id, status="active")
        self.session.add(cart)
        await self.session.commit()
        cart = await self._load(cart.id)
        record_cart_operation("create")
        logger.info(
            "cart_created",
            cart_id=str(cart.id),
            customer_id=str(customer_id) if customer_id else None,
            session_id=session_id,
        )
        return cart, True

    async def get(self, cart_id: UUID) -> Cart:
        record_cart_operation("get")
        return await self._load(cart_id)

    async def add_item(self, cart_id: UUID, payload: CartItemAdd) -> Cart:
        cart = await self._load(cart_id)
        if cart.status != "active":
            raise ConflictError("Cart is not active", {"cart_id": str(cart_id), "status": cart.status})

        product = await self.session.get(Product, payload.product_id)
        if product is None:
            raise NotFoundError("Product", str(payload.product_id))
        if not product.is_active:
            raise ValidationError("Product is not available", {"product_id": str(product.id)})

        existing = next((i for i in cart.items if i.product_id == product.id), None)
        desired = (existing.quantity if existing else 0) + payload.quantity
        if desired > MAX_ITEM_QUANTITY:
            raise ValidationError("Quantity exceeds maximum per item", {"max_quantity": MAX_ITEM_QUANTITY})
        if desired > product.available_quantity:
            raise InventoryError(str(product.id), desired, product.available_quantity)

        if existing:
            existing.quantity = desired
            existing.unit_price = product.price
        else:
            self.session.add(
                CartItem(
                    cart_id=cart.id,
                    product_id=product.id,
                    quantity=payload.quantity,
                    unit_price=product.price,
                    currency=product.currency,
                )
            )

        await self.session.commit()
        cart = await self._load(cart_id)
        await self._refresh_cache(cart)
        record_cart_operation("add_item")
        logger.info(
            "cart_item_added",
            cart_id=str(cart_id),
            product_id=str(product.id),
            sku=product.sku,
            quantity=desired,
        )
        return cart

    async def update_item(self, cart_id: UUID, product_id: UUID, quantity: int) -> Cart:
        cart = await self._load(cart_id)
        item = next((i for i in cart.items if i.product_id == product_id), None)
        if item is None:
            raise NotFoundError("CartItem", str(product_id))

        if quantity > item.product.available_quantity:
            raise InventoryError(str(product_id), quantity, item.product.available_quantity)

        item.quantity = quantity
        await self.session.commit()
        cart = await self._load(cart_id)
        await self._refresh_cache(cart)
        record_cart_operation("update_item")
        logger.info("cart_item_updated", cart_id=str(cart_id), product_id=str(product_id), quantity=quantity)
        return cart

    async def remove_item(self, cart_id: UUID, product_id: UUID) -> Cart:
        cart = await self._load(cart_id)
        item = next((i for i in cart.items if i.product_id == product_id), None)
        if item is None:
            raise NotFoundError("CartItem", str(product_id))

        await self.session.delete(item)
        await self.session.commit()
        cart = await self._load(cart_id)
        await self._refresh_cache(cart)
        record_cart_operation("remove_item")
        logger.info("cart_item_removed", cart_id=str(cart_id), product_id=str(product_id))
        return cart

    async def clear(self, cart_id: UUID) -> Cart:
        cart = await self._load(cart_id)
        for item in list(cart.items):
            await self.session.delete(item)
        await self.session.commit()
        cart = await self._load(cart_id)
        if cart.customer_id:
            await invalidate_cart(str(cart.customer_id))
        record_cart_operation("clear")
        logger.info("cart_cleared", cart_id=str(cart_id))
        return cart

    # ------------------------------------------------------------- serializer
    @staticmethod
    def to_response(cart: Cart) -> CartResponse:
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
            for item in cart.items
        ]
        return CartResponse(
            id=cart.id,
            customer_id=cart.customer_id,
            session_id=cart.session_id,
            status=cart.status,
            items=items,
            subtotal=cart.subtotal,
            item_count=cart.item_count,
            created_at=cart.created_at,
            updated_at=cart.updated_at,
        )