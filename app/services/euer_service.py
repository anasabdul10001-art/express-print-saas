"""
Cash-basis profit (Einnahmenüberschussrechnung, § 4 Abs. 3 EStG): every amount
counts in the period in which the money was received or paid (§ 11 EStG).

How this differs from the management overview (routers/accounting.py):
- goods count when the supplier was PAID (booked purchases), not when they are sold.
  Ordinary trading goods are not among the assets of § 4 Abs. 3 Satz 4 EStG
  whose cost has to wait for the sale. The product costs are therefore NOT used.
- refunds, returned fees and shipping labels count on the day of their own
  transaction (CashEvent), also when the sale was in an earlier period.

Assumptions, shown to the user next to the figures:
- a sale is received when the buyer pays, taken as the order date. Whether the
  money counts then or at eBay's payout is not settled by the law's text.
- fees eBay keeps back count as income in full and as an expense.
- where no transaction day is known, the order date is used and counted.

Not covered: depreciation (AfA) and other assets, VAT payments to or from the
tax office (for VAT-liable sellers), and the shifting of recurring payments
around New Year (§ 11 Abs. 1 Satz 2). These are only pointed out.
"""

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.models.cash_event import CashEvent
from app.models.expense import Expense
from app.models.order import Order
from app.models.purchase import Purchase
from app.services import order_money

BERLIN = ZoneInfo("Europe/Berlin")
ZERO = Decimal("0")
ASSET_HINT_LIMIT = Decimal("800")  # § 6 Abs. 2 EStG (net, without VAT)

INCOME = ("sales", "fee_credits")
COSTS = ("refunds", "sale_fees", "labels", "return_shipping", "goods", "expenses")

LABELS = {
    "sales": "Verkaufserlöse (inkl. Versand vom Käufer)",
    "fee_credits": "Von eBay erstattete Gebühren",
    "refunds": "Erstattungen an Käufer",
    "sale_fees": "Verkaufsgebühren",
    "labels": "Versandetiketten",
    "return_shipping": "Rücksendekosten",
    "goods": "Wareneinkauf (bezahlte Lieferantenrechnungen)",
    "expenses": "Sonstige Ausgaben",
}


def _day(moment) -> date:
    return moment.astimezone(BERLIN).date()


def period_of(day: date, group_by: str) -> tuple[str, str]:
    if group_by == "month":
        return day.strftime("%Y-%m"), day.strftime("%m/%Y")
    if group_by == "quarter":
        quarter = (day.month - 1) // 3 + 1
        return f"{day.year}-Q{quarter}", f"Q{quarter}/{day.year}"
    return str(day.year), str(day.year)


def entries(db: Session, tenant_id, start: date, end: date) -> tuple[list[dict], dict]:
    """Every booking in the period: {day, kind, amount, text}, plus what had to be assumed."""
    lo = datetime.combine(start - timedelta(days=1), datetime.min.time()).replace(tzinfo=BERLIN)
    hi = datetime.combine(end + timedelta(days=2), datetime.min.time()).replace(tzinfo=BERLIN)
    sold_at = func.coalesce(Order.ordered_at, Order.created_at)
    with_events = db.query(CashEvent.order_id).filter(
        CashEvent.tenant_id == tenant_id, CashEvent.booked_on >= start, CashEvent.booked_on <= end)
    orders = (db.query(Order).options(joinedload(Order.items))
              .filter(Order.tenant_id == tenant_id,
                      or_((sold_at >= lo) & (sold_at < hi), Order.id.in_(with_events)))
              .all())

    events: dict = defaultdict(list)
    if orders:
        for event in db.query(CashEvent).filter(CashEvent.tenant_id == tenant_id,
                                                CashEvent.order_id.in_([o.id for o in orders])):
            events[(event.order_id, event.kind)].append(event)

    rows: list[dict] = []
    estimated: set = set()  # orders for which some amount has no transaction day of its own
    sales_without_fees = 0

    def add(day, kind, amount, text, estimate_of=None):
        if amount and start <= day <= end:
            rows.append({"day": day, "kind": kind, "amount": amount, "text": text})
            if estimate_of is not None:
                estimated.add(estimate_of)

    for order in orders:
        sale_day = _day(order.ordered_at or order.created_at)
        reference = order.external_order_ref or str(order.id)[:8]
        if reference.startswith("deleted:"):
            reference = "(Käuferdaten gelöscht)"
        canceled = order.status == "canceled"
        if not canceled and order.ebay_payment_status in ("PENDING", "FAILED"):
            continue  # the buyer has not paid
        if not canceled:
            add(sale_day, "sales", order_money.revenue(order), reference)
            if order.ebay_account_id and order.ebay_fees is None and start <= sale_day <= end:
                sales_without_fees += 1

        returned_fees = order.ebay_fee_credit
        if returned_fees is None:
            full = order.ebay_payment_status == "FULLY_REFUNDED"
            returned_fees = (order.ebay_fees or ZERO) if full else ZERO
        totals = {"sale_fees": ("sale_fee", order.ebay_fees or ZERO), "refunds": ("refund", order_money.refund(order)),
                  "fee_credits": ("fee_credit", returned_fees), "labels": ("label", order_money.labels(order))}
        for kind, (event_kind, total) in totals.items():
            if canceled and kind != "labels":
                continue  # the sale is not counted, so neither is its money back
            dated = events[(order.id, event_kind)]
            for event in dated:
                add(event.booked_on, kind, event.amount, reference)
            # what the order says beyond the dated transactions (typed by hand, or never synced)
            rest = total - sum((e.amount for e in dated), ZERO)
            if rest:
                own_sale_fee = kind == "sale_fees" and not order.ebay_account_id
                add(sale_day, kind, rest, reference, None if own_sale_fee else order.id)
        if not canceled and order.return_shipping_cost:
            add(sale_day, "return_shipping", order.return_shipping_cost, reference, order.id)

    purchases_without_paid_date = 0
    paid_on = func.coalesce(Purchase.paid_date, Purchase.invoice_date)
    for purchase in db.query(Purchase).filter(
            Purchase.tenant_id == tenant_id, Purchase.cancelled_at.is_(None), paid_on >= start, paid_on <= end):
        if purchase.paid_date is None:
            purchases_without_paid_date += 1
        add(purchase.paid_date or purchase.invoice_date, "goods", purchase.total,
            f"{purchase.supplier} {purchase.invoice_number or ''}".strip())

    large, turn_of_year = [], 0
    for expense in db.query(Expense).filter(
            Expense.tenant_id == tenant_id, Expense.expense_date >= start, Expense.expense_date <= end):
        category = expense.category or "Sonstiges"
        add(expense.expense_date, "expenses", expense.amount, f"{category}: {expense.description}")
        if expense.amount > ASSET_HINT_LIMIT:
            large.append(f"{expense.expense_date.strftime('%d.%m.%Y')} {expense.description} ({expense.amount} €)")
        day = expense.expense_date
        if (day.month == 12 and day.day >= 22) or (day.month == 1 and day.day <= 10):
            turn_of_year += 1

    rows.sort(key=lambda r: (r["day"], r["kind"], r["text"]))
    return rows, {
        "orders_with_estimated_dates": len(estimated),
        "sales_without_fees": sales_without_fees,
        "purchases_without_paid_date": purchases_without_paid_date,
        "large_expenses": large,
        "turn_of_year_expenses": turn_of_year,
    }


def warnings_for(checks: dict, totals: dict, vat_mode: str) -> list[str]:
    out = []
    if totals["sales"] and not totals["goods"]:
        out.append("Im Zeitraum gibt es Verkäufe, aber keinen Wareneinkauf. Bitte die bezahlten "
                   "Lieferantenrechnungen unter „Einkäufe“ erfassen, sonst ist der Gewinn zu hoch.")
    if checks["purchases_without_paid_date"]:
        out.append(f"{checks['purchases_without_paid_date']} Einkauf/Einkäufe ohne „Bezahlt am“: "
                   "es wurde das Rechnungsdatum verwendet.")
    if checks["orders_with_estimated_dates"]:
        out.append(f"Bei {checks['orders_with_estimated_dates']} Bestellung(en) fehlt für Gebühren, Erstattung, "
                   "Etikett oder Rücksendung das Zahlungsdatum: es zählt das Bestelldatum. "
                   "Ein neuer eBay-Abgleich kann fehlende Daten ergänzen, soweit eBay sie noch liefert.")
    if checks["sales_without_fees"]:
        out.append(f"Bei {checks['sales_without_fees']} eBay-Verkauf/Verkäufen sind die Gebühren noch unbekannt.")
    if checks["large_expenses"]:
        out.append("Ausgaben über 800 € können ein Wirtschaftsgut sein, das abgeschrieben werden muss "
                   "(Grenze § 6 Abs. 2 EStG: 800 € netto). Bitte prüfen: " + "; ".join(checks["large_expenses"][:10]))
    if checks["turn_of_year_expenses"]:
        out.append(f"{checks['turn_of_year_expenses']} Ausgabe(n) liegen kurz um den Jahreswechsel. Regelmäßig "
                   "wiederkehrende Zahlungen können ins andere Jahr gehören (§ 11 EStG). Bitte prüfen.")
    if vat_mode == "regular":
        out.append("Regelbesteuerung: Zahlungen an das Finanzamt und Erstattungen der Umsatzsteuer gehören auch "
                   "in die EÜR. Zahlungen bitte als Ausgabe erfassen; Erstattungen werden hier nicht erfasst.")
    return out


def report(db: Session, tenant_id, start: date, end: date, group_by: str, vat_mode: str) -> dict:
    rows, checks = entries(db, tenant_id, start, end)

    def blank():
        return {k: ZERO for k in INCOME + COSTS}

    buckets, names, totals = {}, {}, blank()
    for row in rows:
        key, name = period_of(row["day"], group_by)
        names[key] = name
        buckets.setdefault(key, blank())[row["kind"]] += row["amount"]
        totals[row["kind"]] += row["amount"]

    def finish(values):
        income = sum((values[k] for k in INCOME), ZERO)
        costs = sum((values[k] for k in COSTS), ZERO)
        return {**{k: f"{values[k]:.2f}" for k in values}, "income": f"{income:.2f}",
                "costs": f"{costs:.2f}", "surplus": f"{income - costs:.2f}"}

    return {
        "start": start.isoformat(), "end": end.isoformat(), "group_by": group_by,
        "labels": LABELS, "income_keys": list(INCOME), "cost_keys": list(COSTS),
        "periods": [{"label": names[k], **finish(buckets[k])} for k in sorted(buckets)],
        "total": finish(totals),
        "warnings": warnings_for(checks, totals, vat_mode),
        "checks": checks,
    }
