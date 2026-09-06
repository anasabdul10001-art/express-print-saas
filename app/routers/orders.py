from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
from app.core.deps import get_current_user, get_db
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.user import User
from app.schemas.order import OrderCreate, OrderItemOut, OrderOut, OrderSummaryOut
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


@router.get("/summary", response_model=OrderSummaryOut)
def order_summary(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    Powers the dashboard's quick numbers. Boundaries are UTC calendar days/
    months (not the seller's local timezone) - fine for a rough at-a-glance
    figure, but worth knowing if "today" ever looks off by a few hours
    around midnight. Profit here simply skips items with unknown cost data
    rather than voiding the whole total (unlike a single order's
    total_profit, which goes to None if ANY item is unknown) - a dashboard
    estimate is more useful approximate than blank.
    """
    now = datetime.now(timezone.utc)
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_month = start_of_today.replace(day=1)

    def counts_and_profit(since: datetime) -> tuple[int, Decimal]:
        orders_count = (
            db.query(func.count(Order.id))
            .filter(Order.tenant_id == current_user.tenant_id, Order.created_at >= since)
            .scalar()
        ) or 0
        profit = (
            db.query(func.sum((OrderItem.unit_price - OrderItem.purchase_price) * OrderItem.quantity))
            .join(Order, Order.id == OrderItem.order_id)
            .filter(Order.tenant_id == current_user.tenant_id, Order.created_at >= since)
            .scalar()
        ) or Decimal("0")
        return orders_count, profit

    orders_today, profit_today = counts_and_profit(start_of_today)
    orders_month, profit_month = counts_and_profit(start_of_month)

    return OrderSummaryOut(
        orders_today=orders_today,
        profit_today=profit_today,
        orders_month=orders_month,
        profit_month=profit_month,
    )
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

    Stock is checked and decremented here too: an order can't be created
    for more units than are currently on hand, and once created, those
    units are immediately removed from stock_quantity. All products are
    checked BEFORE any stock is touched, so a failure on item 3 of 3
    never leaves items 1-2 partially decremented.
    """
    products_by_id = {}
    for item_in in payload.items:
        product = (
            db.query(Product)
            .filter(Product.id == item_in.product_id, Product.tenant_id == current_user.tenant_id)
            .first()
        )
        if not product:
            raise HTTPException(status_code=404, detail=f"Produkt {item_in.product_id} nicht gefunden.")
        if product.stock_quantity < item_in.quantity:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Nicht genügend Lagerbestand für '{product.title}': "
                    f"{product.stock_quantity} verfügbar, {item_in.quantity} angefordert."
                ),
            )
        products_by_id[item_in.product_id] = product

    order = Order(tenant_id=current_user.tenant_id, external_order_ref=payload.external_order_ref, status="new")
    db.add(order)
    db.flush()

    for item_in in payload.items:
        product = products_by_id[item_in.product_id]

        db.add(OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=item_in.quantity,
            unit_price=product.selling_price,
            purchase_price=product.purchase_price,
            shelf_location=product.shelf_location,
        ))
        product.stock_quantity -= item_in.quantity

    db.commit()
    order = (
        db.query(Order)
        .options(joinedload(Order.items).joinedload(OrderItem.product))
        .filter(Order.id == order.id)
        .first()
    )
    return _serialize_order(order)