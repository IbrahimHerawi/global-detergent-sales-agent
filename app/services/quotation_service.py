"""Deterministic quotation cart operations and lifecycle invalidation."""

from __future__ import annotations

from app.core.exceptions import InvalidQuantityError, ProductNotFoundError
from app.repositories.product_repository import ProductRepository
from app.schemas.product import Product
from app.schemas.quotation import QuoteCartItem
from app.schemas.session import ConversationSession, ConversationState


class QuotationService:
    """Manage quotation state using authoritative catalog product identities."""

    __slots__ = ("_product_repository",)

    def __init__(self, product_repository: ProductRepository | None = None) -> None:
        self._product_repository = (
            product_repository if product_repository is not None else ProductRepository()
        )

    def add_item(
        self,
        session: ConversationSession,
        product_id: str,
        quantity: object,
    ) -> None:
        """Add an active product, incrementing an existing cart quantity."""
        product = self._require_active_product(product_id)
        valid_quantity = _validate_quantity(quantity)
        new_cart = [item.model_copy(deep=True) for item in session.cart]

        existing = _find_cart_item(new_cart, product.id)
        if existing is None:
            new_cart.append(QuoteCartItem(product_id=product.id, quantity=valid_quantity))
        else:
            existing.quantity += valid_quantity

        _commit_cart_change(session, new_cart)

    def update_item_quantity(
        self,
        session: ConversationSession,
        product_id: str,
        quantity: object,
    ) -> None:
        """Replace the quantity of an active product already in the cart."""
        product = self._require_active_product(product_id)
        valid_quantity = _validate_quantity(quantity)
        existing = _find_cart_item(session.cart, product.id)
        if existing is None:
            raise ProductNotFoundError("Product is not present in the quotation cart")
        if existing.quantity == valid_quantity:
            return

        new_cart = [
            QuoteCartItem(
                product_id=item.product_id,
                quantity=valid_quantity if item.product_id == product.id else item.quantity,
            )
            for item in session.cart
        ]
        _commit_cart_change(session, new_cart)

    def remove_item(self, session: ConversationSession, product_id: str) -> None:
        """Remove an active product that is currently in the cart."""
        product = self._require_active_product(product_id)
        if _find_cart_item(session.cart, product.id) is None:
            raise ProductNotFoundError("Product is not present in the quotation cart")

        new_cart = [
            item.model_copy(deep=True)
            for item in session.cart
            if item.product_id != product.id
        ]
        _commit_cart_change(session, new_cart)

    def clear_cart(self, session: ConversationSession) -> None:
        """Remove every cart item, leaving an already-empty cart unchanged."""
        if not session.cart:
            return
        _commit_cart_change(session, [])

    def _require_active_product(self, product_id: str) -> Product:
        """Resolve a catalog product and reject unknown or inactive records."""
        if not isinstance(product_id, str) or not product_id.strip():
            raise ProductNotFoundError("Missing or invalid product ID")
        product = self._product_repository.get_by_id(product_id)
        if product is None or not product.active:
            raise ProductNotFoundError("Unknown or inactive product requested")
        return product


def _validate_quantity(quantity: object) -> int:
    """Accept positive integers while rejecting booleans and coercion."""
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
        raise InvalidQuantityError("Quantity must be a strict positive integer")
    return quantity


def _find_cart_item(cart: list[QuoteCartItem], product_id: str) -> QuoteCartItem | None:
    return next((item for item in cart if item.product_id == product_id), None)


def _commit_cart_change(
    session: ConversationSession,
    new_cart: list[QuoteCartItem],
) -> None:
    """Commit one validated change and revoke stale quote authorization."""
    session.cart = new_cart
    session.quote_revision += 1
    session.prepared_preview_fingerprint = None
    session.preview_delivered = False
    session.preview_originating_turn_id = None
    session.quote_confirmed = False
    session.confirmation_turn_id = None
    session.last_generated_quote = None
    session.state = (
        ConversationState.BUILDING_QUOTE
        if new_cart
        else ConversationState.PRODUCT_DISCUSSION
    )


__all__ = ["QuotationService"]
