"""
Accounting overview: income from orders, eBay fees, cost of goods and the
manually entered expenses, for any period, grouped by day/week/month/year.

Definitions (management view, not a tax return):
- revenue      = sum(unit_price * quantity) of non-canceled orders, by sale date
- refunds      = money paid back to buyers (eBay payment summary / manual)
- return_shipping = what the seller paid to get returned items back
- ebay_fees    = fees eBay charged for those orders (Finances API)
- goods_cost   = per order item the cost snapshot (purchase + supplier
                 shipping + packaging) * quantity; 0 for a refunded order
                 whose item came back to stock (nothing was lost)
- expenses     = manually entered costs (electricity, internet, ...) by date
- net_profit   = revenue - refunds - ebay_fees - goods_cost - return_shipping - expenses
Do NOT also enter goods purchases as expenses: they are already counted
through the product costs.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.core.deps import get_current_user, get_db
from app.models.expense import Expense
from app.models.order import Order
from app.models.user import User

router = APIRouter(prefix="/accounting", tags=["accounting"])

BERLIN = ZoneInfo("Europe/Berlin")
ZERO = Decimal("0")
MAX_DAYS = 3660


def _sale_date(order: Order) -> date:
    moment = order.ordered_at or order.created_at
    return moment.astimezone(BERLIN).date()


def _period_key(day: date, group_by: str) -> tuple[str, str]:
    """(sortable key, label) of the bucket a day belongs to."""
    if group_by == "day":
        return day.isoformat(), day.strftime("%d.%m.%Y")
    if group_by == "week":
        monday = day - timedelta(days=day.weekday())
        return monday.isoformat(), f"KW {monday.isocalendar()[1]} ({monday.strftime('%d.%m.')})"
    if group_by == "month":
        return day.strftime("%Y-%m"), day.strftime("%m/%Y")
    if group_by == "year":
        return str(day.year), str(day.year)
    return "all", "Gesamt"


def _blank():
    return {"revenue": ZERO, "refunds": ZERO, "return_shipping": ZERO, "ebay_fees": ZERO, "goods_cost": ZERO, "expenses": ZERO, "orders": 0}


@router.get("/summary")
def summary(
    start: date,
    end: date,
    group_by: Literal["day", "week", "month", "year", "none"] = Query("none"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if end < start:
        raise HTTPException(status_code=400, detail="Das Enddatum liegt vor dem Startdatum.")
    if (end - start).days > MAX_DAYS:
        raise HTTPException(status_code=400, detail="Zeitraum zu lang (maximal 10 Jahre).")

    # Pull a day of slack on each side, then filter by the Berlin date.
    lo = datetime.combine(start - timedelta(days=1), datetime.min.time()).replace(tzinfo=BERLIN)
    hi = datetime.combine(end + timedelta(days=2), datetime.min.time()).replace(tzinfo=BERLIN)
    orders = (
        db.query(Order)
        .options(joinedload(Order.items))
        .filter(
            Order.tenant_id == current_user.tenant_id,
            Order.status != "canceled",
            func.coalesce(Order.ordered_at, Order.created_at) >= lo,
            func.coalesce(Order.ordered_at, Order.created_at) < hi,
        )
        .all()
    )

    buckets: dict[str, dict] = defaultdict(_blank)
    labels: dict[str, str] = {}
    totals = _blank()
    missing_cost = missing_fees = refunded_orders = 0

    def add(day: date, field: str, amount, orders_inc: int = 0):
        key, label = _period_key(day, group_by)
        labels[key] = label
        buckets[key][field] += amount
        buckets[key]["orders"] += orders_inc
        totals[field] += amount
        totals["orders"] += orders_inc

    for order in orders:
        day = _sale_date(order)
        if not (start <= day <= end):
            continue
        revenue = sum(((i.unit_price or ZERO) * i.quantity for i in order.items), ZERO)
        goods = sum(((i.purchase_price or ZERO) * i.quantity for i in order.items), ZERO)
        if any(i.purchase_price is None for i in order.items):
            missing_cost += 1
        if order.ebay_fees is None:
            missing_fees += 1
        refund = order.refund_amount or ZERO
        if order.item_returned_to_stock and refund > 0:
            goods = ZERO
        add(day, "revenue", revenue, 1)
        add(day, "refunds", refund)
        add(day, "return_shipping", order.return_shipping_cost or ZERO)
        if refund > 0:
            refunded_orders += 1
        add(day, "goods_cost", goods)
        add(day, "ebay_fees", order.ebay_fees or ZERO)

    by_category: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for expense in (
        db.query(Expense)
        .filter(Expense.tenant_id == current_user.tenant_id, Expense.expense_date >= start, Expense.expense_date <= end)
        .all()
    ):
        add(expense.expense_date, "expenses", expense.amount)
        by_category[expense.category or "Sonstiges"] += expense.amount

    def finish(row: dict) -> dict:
        net = (
            row["revenue"] - row["refunds"] - row["ebay_fees"] - row["goods_cost"]
            - row["return_shipping"] - row["expenses"]
        )
        out = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in row.items()}
        out["net_profit"] = str(net)
        return out

    series = []
    if group_by != "none":
        for key in sorted(buckets):
            series.append({"period": key, "label": labels[key], **finish(buckets[key])})

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "group_by": group_by,
        "totals": {
            **finish(totals),
            "orders_missing_cost": missing_cost,
            "orders_missing_fees": missing_fees,
            "orders_refunded": refunded_orders,
        },
        "expenses_by_category": [
            {"category": c, "amount": str(a)} for c, a in sorted(by_category.items(), key=lambda x: -x[1])
        ],
        "series": series,
    }
