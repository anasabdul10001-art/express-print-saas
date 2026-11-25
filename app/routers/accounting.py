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
- shipping_labels = label costs bought through eBay (Finances API)
- net_profit   = revenue - refunds - ebay_fees - goods_cost - return_shipping - shipping_labels - expenses
Do NOT also enter goods purchases as expenses: they are already counted
through the product costs.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.core.deps import get_current_user, get_db
from app.models.expense import Expense
from app.models.order import Order
from app.models.tenant import Tenant
from app.models.user import User
from app.services import order_money

router = APIRouter(prefix="/accounting", tags=["accounting"])

BERLIN = ZoneInfo("Europe/Berlin")
ZERO = Decimal("0")
MAX_DAYS = 3660


VAT_RATE = Decimal("19")


class VatSettingsIn(BaseModel):
    vat_mode: Literal["kleinunternehmer", "regular"]


def _tenant(db: Session, user: User) -> Tenant:
    return db.query(Tenant).filter(Tenant.id == user.tenant_id).one()


@router.get("/vat-settings")
def get_vat_settings(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return {"vat_mode": _tenant(db, current_user).vat_mode, "rate": str(VAT_RATE)}


@router.put("/vat-settings")
def set_vat_settings(payload: VatSettingsIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant = _tenant(db, current_user)
    tenant.vat_mode = payload.vat_mode
    db.commit()
    return {"vat_mode": tenant.vat_mode, "rate": str(VAT_RATE)}


def _vat_numbers(totals: dict, fees_include_vat: bool = True) -> dict:
    """19% VAT figures for one period. All amounts are assumed GROSS (eBay prices, purchase prices,
    labels and fees include VAT), so VAT = gross * 19/119.

    Input VAT (Vorsteuer) = VAT inside goods, labels, return shipping, manual expenses and - when
    `fees_include_vat` - eBay fees (eBay bills German VAT on its fees to VAT-liable business sellers).
    It is an ESTIMATE from these amounts, not from the real invoices."""
    factor = VAT_RATE / (VAT_RATE + Decimal("100"))
    gross_sales = totals["revenue"] - totals["refunds"]
    sales_vat = (gross_sales * factor).quantize(Decimal("0.01"))
    parts = {
        "goods": totals["goods_cost"],
        "labels": totals["shipping_labels"],
        "return_shipping": totals["return_shipping"],
        "expenses": totals["expenses"],
        "ebay_fees": totals["ebay_fees"] if fees_include_vat else ZERO,
    }
    input_vat_parts = {k: (v * factor).quantize(Decimal("0.01")) for k, v in parts.items()}
    input_vat = sum(input_vat_parts.values(), ZERO)
    return {
        "rate": str(VAT_RATE),
        "gross_sales_after_refunds": gross_sales,
        "net_sales": gross_sales - sales_vat,
        "sales_vat": sales_vat,
        "input_vat_parts": input_vat_parts,
        "input_vat": input_vat,
        "payable": sales_vat - input_vat,
    }


def _vat_block(totals: dict, fees_include_vat: bool = True) -> dict:
    v = _vat_numbers(totals, fees_include_vat)
    return {
        "rate": v["rate"],
        "gross_sales_after_refunds": str(v["gross_sales_after_refunds"]),
        "net_sales": str(v["net_sales"]),
        "sales_vat": str(v["sales_vat"]),
        "input_vat_estimate": str(v["input_vat"]),
        "payable_estimate": str(v["payable"]),
    }


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
    return {"revenue": ZERO, "refunds": ZERO, "return_shipping": ZERO, "shipping_labels": ZERO, "ebay_fees": ZERO, "goods_cost": ZERO, "expenses": ZERO, "orders": 0}


@router.get("/summary")
def summary(
    start: date,
    end: date,
    group_by: Literal["day", "week", "month", "year", "none"] = Query("none"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant_mode = _tenant(db, current_user).vat_mode
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
        revenue = order_money.revenue(order)
        goods = order_money.goods(order)
        if any(i.purchase_price is None for i in order.items):
            missing_cost += 1
        if order.ebay_fees is None:
            missing_fees += 1
        refund = order_money.refund(order)
        add(day, "revenue", revenue, 1)
        add(day, "refunds", refund)
        add(day, "return_shipping", order.return_shipping_cost or ZERO)
        add(day, "shipping_labels", order_money.labels(order))
        if refund > 0:
            refunded_orders += 1
        add(day, "goods_cost", goods)
        add(day, "ebay_fees", order_money.fees(order))

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
            - row["return_shipping"] - row["shipping_labels"] - row["expenses"]
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
        "vat_mode": tenant_mode,
        "vat": _vat_block(totals) if tenant_mode == "regular" else None,
        "expenses_by_category": [
            {"category": c, "amount": str(a)} for c, a in sorted(by_category.items(), key=lambda x: -x[1])
        ],
        "series": series,
    }


def _de(value: Decimal) -> str:
    return f"{value:.2f}".replace(".", ",")


@router.get("/vat-report")
def vat_report(
    start: date,
    end: date,
    group_by: Literal["month", "quarter", "year"] = Query("month"),
    fees_include_vat: bool = Query(True),
    output: Literal["json", "csv"] = Query("json"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Preparation for the VAT advance return (USt-Voranmeldung) per month / quarter / year.

    Only for regular (19%) taxpayers. Not a tax return: Vorsteuer is estimated from the gross
    amounts we know; the Steuerberater / ELSTER uses the real invoices. Assumes every sale is
    domestic at 19% (no reduced rate, no EU one-stop-shop, no reverse charge)."""
    if _tenant(db, current_user).vat_mode != "regular":
        raise HTTPException(status_code=400, detail="Die USt-Auswertung gibt es nur bei Regelbesteuerung (19 %).")
    # Quarters are built from months so every bucket uses exactly the same figures as the overview.
    data = summary(start=start, end=end, group_by="year" if group_by == "year" else "month",
                   current_user=current_user, db=db)
    fields = ("revenue", "refunds", "return_shipping", "shipping_labels", "ebay_fees", "goods_cost", "expenses")
    buckets: dict[str, dict] = {}
    labels: dict[str, str] = {}
    for row in data["series"]:
        key, label = row["period"], row["label"]
        if group_by == "quarter":
            year, month = key.split("-")
            q = (int(month) - 1) // 3 + 1
            key, label = f"{year}-Q{q}", f"Q{q}/{year}"
        bucket = buckets.setdefault(key, {f: ZERO for f in fields})
        labels[key] = label
        for f in fields:
            bucket[f] += Decimal(row[f])

    rows, total = [], {f: ZERO for f in fields}
    for key in sorted(buckets):
        v = _vat_numbers(buckets[key], fees_include_vat)
        rows.append((labels[key], v))
        for f in fields:
            total[f] += buckets[key][f]
    total_v = _vat_numbers(total, fees_include_vat)

    def as_json(label, v):
        return {
            "period": label,
            "gross_sales_after_refunds": str(v["gross_sales_after_refunds"]),
            "net_sales": str(v["net_sales"]),
            "sales_vat": str(v["sales_vat"]),
            "input_vat_parts": {k: str(x) for k, x in v["input_vat_parts"].items()},
            "input_vat": str(v["input_vat"]),
            "payable": str(v["payable"]),
        }

    if output == "csv":
        import csv, io
        from fastapi.responses import Response
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Zeitraum", "Brutto-Umsatz nach Erstattungen", "Netto-Umsatz (Kz 81)", "USt 19 % (Kz 81)",
                    "Vorsteuer Ware", "Vorsteuer Versandetiketten", "Vorsteuer Rückversand", "Vorsteuer Ausgaben",
                    "Vorsteuer eBay-Gebühren", "Vorsteuer gesamt (Kz 66)", "Zahllast (Kz 83)"])
        for label, v in rows + [("Summe", total_v)]:
            p = v["input_vat_parts"]
            w.writerow([label, _de(v["gross_sales_after_refunds"]), _de(v["net_sales"]), _de(v["sales_vat"]),
                        _de(p["goods"]), _de(p["labels"]), _de(p["return_shipping"]), _de(p["expenses"]),
                        _de(p["ebay_fees"]), _de(v["input_vat"]), _de(v["payable"])])
        w.writerow([])
        w.writerow(["Hinweis: Vorbereitung, keine Steuererklärung. Vorsteuer geschätzt aus Brutto-Beträgen; "
                    "Annahme: alle Umsätze in Deutschland zu 19 %. Bitte mit Steuerberater/ELSTER abgleichen."])
        return Response(
            content="﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="ust-vorbereitung-{start}-{end}.csv"'},
        )

    return {
        "start": start.isoformat(), "end": end.isoformat(), "group_by": group_by,
        "fees_include_vat": fees_include_vat,
        "rows": [as_json(label, v) for label, v in rows],
        "total": as_json("Summe", total_v),
    }
