from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from app.core.deps import get_current_user, get_db
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.schemas.order import OrderCreate, OrderItemOut, OrderOut
router = APIRouter(prefix="/orders", tags=["orders"])
def _serialize_order(order: Order) -> OrderOut:
    """
    Builds OrderItemOut manually rather than relying on automatic ORM->schema
    mapping, because image_url/shelf_location intentionally come from the
    linked Product (live data), not straight off the OrderItem row -
    see the note in models/order.py. Profit uses the item's OWN unit_price
    and purchase_price snapshots (not live product prices), so a later
    price change never rewrites the profit of a past order.
    """
    items_out = []
    total_profit = Decimal("0")
    profit_known = True

    for item in order.items:
        product = item.product

        if item.unit_price is not None and item.purchase_price is not None:
            profit = (item.unit_price - item.purchase_price) * item.quantity
        else:
            profit = None
            profit_known = False

        if profit is not None:
            total_profit += profit

        items_out.append(
            OrderItemOut(
                id=item.id,
                product_title=product.title if product else "Produkt nicht mehr vorhanden",
                quantity=item.quantity,
                unit_price=item.unit_price,
                image_url=product.image_url if product else None,
                # Prefer the product's CURRENT shelf location; fall back to
                # the order_item's snapshot only if the product was removed.
                shelf_location=(product.shelf_location if product else None) or item.shelf_location,
                profit=profit,
            )
        )
    return OrderOut(
        id=order.id,
        external_order_ref=order.external_order_ref,
        status=order.status,
        items=items_out,
        total_profit=total_profit if profit_known else None,
    )
@router.get("", response_model=list[OrderOut])
def list_orders(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Feeds the dashboard's Orders table: thumbnail, name, shelf location, status."""
    orders = (
        db.query(Order)
        .filter(Order.tenant_id == current_user.tenant_id)
        .options(joinedload(Order.items).joinedload(OrderItem.product))
        .order_by(Order.created_at.desc())
        .all()
    )
    return [_serialize_order(o) for o in orders]
@router.post("", response_model=OrderOut, status_code=201)
def create_order(
    payload: OrderCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Creates a real order from actual products the seller owns. Price and
    shelf_location are always pulled fresh from the product at creation
    time - never trusted from the request - so a seller can't be tricked
    into an order with a wrong price, and the shelf_location/purchase_price
    snapshots reflect what was true when the order came in.
    """
    order = Order(tenant_id=current_user.tenant_id, external_order_ref=payload.external_order_ref, status="new")
    db.add(order)
    db.flush()

    for item_in in payload.items:
        product = (
            db.query(Product)
            .filter(Product.id == item_in.product_id, Product.tenant_id == current_user.tenant_id)
            .first()
        )
        if not product:
            raise HTTPException(status_code=404, detail=f"Produkt {item_in.product_id} nicht gefunden.")

        db.add(OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=item_in.quantity,
            unit_price=product.selling_price,
            purchase_price=product.purchase_price,
            shelf_location=product.shelf_location,
        ))

    db.commit()
    order = (
        db.query(Order)
        .options(joinedload(Order.items).joinedload(OrderItem.product))
        .filter(Order.id == order.id)
        .first()
    )
    return _serialize_order(order)