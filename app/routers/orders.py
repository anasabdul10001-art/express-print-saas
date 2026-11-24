import uuid
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
from app.core.deps import get_current_user, get_db
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.stock_movement import StockMovement
from app.models.user import User
from app.services import order_money
from app.schemas.order import OrderCreate, OrderItemOut, OrderOut, OrderSummaryOut, ProfitHistoryPoint, RecipientAddressOut, RefundUpdate
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
    goods_cost = Decimal("0")

    for item in order.items:
        product = item.product

        if item.unit_price is not None and item.purchase_price is not None:
            profit = (item.unit_price - item.purchase_price) * item.quantity
        else:
            profit = None
            profit_known = False

        if profit is not None:
            total_profit += profit
            goods_cost += item.purchase_price * item.quantity

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
    recipient_address = None
    if order.recipient_name or order.recipient_street1:
        recipient_address = RecipientAddressOut(
            name=order.recipient_name,
            street1=order.recipient_street1,
            street2=order.recipient_street2,
            city=order.recipient_city,
            state=order.recipient_state,
            zip=order.recipient_zip,
            country_code=order.recipient_country_code,
            phone=order.recipient_phone,
        )

    # Same rules as the accounting page (app/services/order_money.py): what the buyer
    # actually paid, minus refund, fees, goods cost and return shipping.
    if profit_known:
        total_profit = (
            order_money.revenue(order) - order_money.refund(order) - order_money.fees(order)
            - order_money.goods(order) - (order.return_shipping_cost or Decimal("0"))
        )

    return OrderOut(
        id=order.id,
        external_order_ref=order.external_order_ref,
        status=order.status,
        items=items_out,
        total_profit=total_profit if profit_known else None,
        ebay_fees=order.ebay_fees,
        ordered_at=order.ordered_at or order.created_at,
        refund_amount=order.refund_amount,
        item_returned_to_stock=bool(order.item_returned_to_stock),
        return_shipping_cost=order.return_shipping_cost,
        ebay_account_name=order.ebay_account.ebay_user_id if order.ebay_account else None,
        recipient_address=recipient_address,
        tracking_number=order.tracking_number,
    )
@router.get("", response_model=list[OrderOut])
def list_orders(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Feeds the dashboard's Orders table: thumbnail, name, shelf location, status."""
    orders = (
        db.query(Order)
        .filter(Order.tenant_id == current_user.tenant_id)
        .options(joinedload(Order.items).joinedload(OrderItem.product), joinedload(Order.ebay_account))
        .order_by(func.coalesce(Order.ordered_at, Order.created_at).desc(), Order.created_at.desc())
        .all()
    )
    return [_serialize_order(o) for o in orders]


BERLIN = ZoneInfo("Europe/Berlin")


def _order_profit(order: Order) -> Decimal:
    """Profit of one order by the same rules as the accounting page (fees, refunds, returns)."""
    return (
        order_money.revenue(order) - order_money.refund(order) - order_money.fees(order)
        - order_money.goods(order) - (order.return_shipping_cost or Decimal("0"))
    )


def _sale_day(order: Order):
    return (order.ordered_at or order.created_at).astimezone(BERLIN).date()


def _recent_orders(db: Session, tenant_id, since_day):
    """Non-canceled orders sold on or after since_day (Berlin date), with their items."""
    lo = datetime.combine(since_day - timedelta(days=1), datetime.min.time()).replace(tzinfo=BERLIN)
    orders = (
        db.query(Order)
        .options(joinedload(Order.items))
        .filter(
            Order.tenant_id == tenant_id,
            Order.status != "canceled",
            func.coalesce(Order.ordered_at, Order.created_at) >= lo,
        )
        .all()
    )
    return [o for o in orders if _sale_day(o) >= since_day]


@router.get("/summary", response_model=OrderSummaryOut)
def order_summary(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    Powers the dashboard's quick numbers, by the Berlin calendar day/month of the sale. Profit
    follows the accounting page's rules (eBay's real amounts, fees, refunds, returns); items
    with unknown cost simply count as 0 cost - an estimate is more useful than blank.
    """
    today = datetime.now(BERLIN).date()
    orders = _recent_orders(db, current_user.tenant_id, today.replace(day=1))
    todays = [o for o in orders if _sale_day(o) == today]
    return OrderSummaryOut(
        orders_today=len(todays),
        profit_today=sum((_order_profit(o) for o in todays), Decimal("0")),
        orders_month=len(orders),
        profit_month=sum((_order_profit(o) for o in orders), Decimal("0")),
    )


@router.get("/profit-history", response_model=list[ProfitHistoryPoint])
def profit_history(
    days: int = 14,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Daily profit totals for the last `days` days (including today), powering the dashboard's
    trend chart. Days with no orders come back as 0 so the x-axis stays evenly spaced.
    """
    days = max(1, min(days, 90))
    start_date = datetime.now(BERLIN).date() - timedelta(days=days - 1)
    by_day: dict = {}
    for order in _recent_orders(db, current_user.tenant_id, start_date):
        by_day[_sale_day(order)] = by_day.get(_sale_day(order), Decimal("0")) + _order_profit(order)
    return [
        ProfitHistoryPoint(date=start_date + timedelta(days=i), profit=by_day.get(start_date + timedelta(days=i), Decimal("0")))
        for i in range(days)
    ]


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
    never leaves items 1-2 partially decremented. Each decrement is also
    logged as a StockMovement row for the audit trail.
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
            purchase_price=product.unit_cost,
            shelf_location=product.shelf_location,
        ))
        product.stock_quantity -= item_in.quantity

        db.add(StockMovement(
            tenant_id=current_user.tenant_id,
            product_id=product.id,
            quantity_change=-item_in.quantity,
            reason="order",
            reference=payload.external_order_ref,
        ))

    db.commit()
    order = (
        db.query(Order)
        .options(joinedload(Order.items).joinedload(OrderItem.product))
        .filter(Order.id == order.id)
        .first()
    )
    return _serialize_order(order)


@router.patch("/{order_id}/refund", response_model=OrderOut)
def update_refund(
    order_id: uuid.UUID,
    payload: RefundUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Sets the return details of an order (refund amount, item back in stock, return shipping)."""
    order = (
        db.query(Order)
        .options(joinedload(Order.items).joinedload(OrderItem.product), joinedload(Order.ebay_account))
        .filter(Order.id == order_id, Order.tenant_id == current_user.tenant_id)
        .first()
    )
    if not order:
        raise HTTPException(status_code=404, detail="Bestellung nicht gefunden.")
    was_returned = bool(order.item_returned_to_stock)
    for field in payload.model_fields_set:
        setattr(order, field, getattr(payload, field))
    # Marking the item as back (and resellable) puts it on the shelf again; unmarking takes it off.
    now_returned = bool(order.item_returned_to_stock)
    if now_returned != was_returned:
        sign = 1 if now_returned else -1
        for item in order.items:
            if item.product is None:
                continue
            item.product.stock_quantity = max(0, item.product.stock_quantity + sign * item.quantity)
            db.add(StockMovement(
                tenant_id=order.tenant_id, product_id=item.product.id, quantity_change=sign * item.quantity,
                reason="manual_correction", reference=f"return {order.external_order_ref or order.id}",
            ))
    db.commit()
    db.refresh(order)
    return _serialize_order(order)
