"""Filling help for the German VAT advance return (Umsatzsteuer-Voranmeldung 2026,
form USt 1 A): line and Kennzahl numbers as on the official form.

A preparation aid, not a tax return - nothing is sent anywhere. Input VAT is never
estimated: it is what the seller reads off each invoice. Whatever is missing is
listed as an open point and the figures count as incomplete.
"""

from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models.cash_event import CashEvent
from app.models.order import Order
from app.services import order_money

FORM_YEAR = 2026
RATE = Decimal("0.19")
ZERO = Decimal("0")
BERLIN = ZoneInfo("Europe/Berlin")

EXPENSE_TREATMENTS = ("de_vat", "no_vat", "eu_service", "other_13b", "eu_goods", "import_vat")
PURCHASE_TREATMENTS = ("de_vat", "no_vat", "eu_goods", "third_country")
PERIODS = tuple(f"Q{q}" for q in range(1, 5)) + tuple(f"{m:02d}" for m in range(1, 13))


def period_range(period: str) -> tuple[date, date]:
    if period.startswith("Q"):
        first = (int(period[1]) - 1) * 3 + 1
        months = 3
    else:
        first, months = int(period), 1
    start = date(FORM_YEAR, first, 1)
    after = first + months
    next_start = date(FORM_YEAR + 1, 1, 1) if after > 12 else date(FORM_YEAR, after, 1)
    return start, next_start - timedelta(days=1)


def cents(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def whole_euros(value: Decimal) -> Decimal:
    """The form takes assessment bases in whole euros; the cents are cut off."""
    return value.quantize(Decimal("1"), rounding=ROUND_DOWN)


def vat_inside(gross: Decimal) -> Decimal:
    """The 19 % contained in a gross amount."""
    return cents(gross * RATE / (1 + RATE))


def _day(moment) -> date:
    return moment.astimezone(BERLIN).date()


def sales(db: Session, tenant_id, start: date, end: date) -> dict:
    """Gross sales of the period and the refunds PAID in the period - also for sales of
    earlier periods (a later refund corrects the VAT when it happens, § 17 UStG)."""
    lo = datetime.combine(start - timedelta(days=1), datetime.min.time()).replace(tzinfo=BERLIN)
    hi = datetime.combine(end + timedelta(days=2), datetime.min.time()).replace(tzinfo=BERLIN)
    sold_at = func.coalesce(Order.ordered_at, Order.created_at)

    sold, foreign, no_country = ZERO, 0, 0
    for order in db.query(Order).options(joinedload(Order.items)).filter(
            Order.tenant_id == tenant_id, Order.status != "canceled", sold_at >= lo, sold_at < hi):
        if not start <= _day(order.ordered_at or order.created_at) <= end:
            continue
        if order.ebay_payment_status in ("PENDING", "FAILED"):
            continue  # the buyer has not paid
        sold += order_money.revenue(order)
        country = (order.recipient_country_code or "").upper()
        if not country:
            no_country += 1
        elif country != "DE":
            foreign += 1

    # Every order with money paid back: the dated transactions decide the period.
    events: dict = {}
    for event in db.query(CashEvent).filter(CashEvent.tenant_id == tenant_id, CashEvent.kind == "refund"):
        events.setdefault(event.order_id, []).append(event)
    candidates = db.query(Order).options(joinedload(Order.items)).filter(
        Order.tenant_id == tenant_id, Order.status != "canceled",
        (Order.refund_amount > 0) | Order.id.in_(events.keys()))

    refunded, undated, mismatched = ZERO, [], []
    for order in candidates:
        dated = events.get(order.id, [])
        refunded += sum((e.amount for e in dated if start <= e.booked_on <= end), ZERO)
        rest = order_money.refund(order) - sum((e.amount for e in dated), ZERO)
        sale_day = _day(order.ordered_at or order.created_at)
        reference = order.external_order_ref or str(order.id)[:8]
        if reference.startswith("deleted:"):
            reference = "(Käuferdaten gelöscht)"
        entry = {"id": str(order.id), "reference": reference, "sale_date": sale_day.isoformat(), "amount": str(rest),
                 "refund_date": order.refund_date.isoformat() if order.refund_date else None}
        if rest > 0:
            if order.refund_date is None:
                if sale_day <= end:  # it can only belong to this or a later period
                    undated.append(entry)
            elif start <= order.refund_date <= end:
                refunded += rest
                undated.append(entry)  # stays listed so the date can still be corrected
        elif rest < 0 and any(start <= e.booked_on <= end for e in dated):
            mismatched.append(reference)

    return {"sold": sold, "refunded": refunded, "net_gross": sold - refunded, "manual_refunds": undated,
            "mismatched": mismatched, "foreign_orders": foreign, "orders_without_country": no_country}


def compute(*, regular: bool, sales_gross: Decimal, sums: dict, input_vat: Decimal,
            advance_payment: Decimal) -> list[dict]:
    """The form's lines in form order. `sums` holds the amounts per treatment that need
    tax computed from them (§ 13b, EU goods, import VAT); `input_vat` is the sum of the
    VAT shown on the domestic invoices (Kz 66)."""
    s = lambda key: sums.get(key, ZERO)
    rows: list[dict] = []

    def row(line, kz, label, value, kind, source):
        rows.append({"line": line, "kz": kz, "label": label, "kind": kind, "source": source,
                     "value": str(int(value)) if kind == "base" else str(cents(value))})

    tax_81 = tax_89 = ZERO
    if regular:
        base_81 = whole_euros(sales_gross - vat_inside(sales_gross))
        tax_81 = cents(base_81 * RATE)
        row(13, "81", "Steuerpflichtige Umsätze zu 19 % – Bemessungsgrundlage", base_81, "base", "sales")
        row(13, None, "Steuer dazu (rechnet ELSTER)", tax_81, "tax", "sales")

    if s("eu_goods"):
        base_89 = whole_euros(s("eu_goods"))
        tax_89 = cents(base_89 * RATE)
        row(25, "89", "Innergemeinschaftliche Erwerbe zu 19 % – Bemessungsgrundlage", base_89, "base", "invoices")
        row(25, None, "Steuer dazu (rechnet ELSTER)", tax_89, "tax", "invoices")

    tax_47 = cents(s("eu_service") * RATE)
    if s("eu_service"):
        row(30, "46", "§ 13b: Leistungen eines Unternehmers aus der EU – Bemessungsgrundlage",
            whole_euros(s("eu_service")), "base", "invoices")
        row(30, "47", "§ 13b: Steuer dazu", tax_47, "tax", "invoices")
    tax_85 = cents(s("other_13b") * RATE)
    if s("other_13b"):
        row(32, "84", "§ 13b: andere Leistungen – Bemessungsgrundlage", whole_euros(s("other_13b")), "base", "invoices")
        row(32, "85", "§ 13b: Steuer dazu", tax_85, "tax", "invoices")

    owed = tax_81 + tax_89 + tax_47 + tax_85
    deductible = ZERO
    if regular:
        row(38, "66", "Vorsteuer aus Rechnungen anderer Unternehmer", input_vat, "tax", "invoices")
        deductible = input_vat
        for line, kz, label, value in (
            (39, "61", "Vorsteuer aus innergemeinschaftlichem Erwerb", tax_89),
            (40, "62", "Entstandene Einfuhrumsatzsteuer", cents(s("import_vat"))),
            (41, "67", "Vorsteuer aus Leistungen nach § 13b", tax_47 + tax_85),
        ):
            if value:
                row(line, kz, label, value, "tax", "invoices")
                deductible += value
        if advance_payment:
            row(49, "39", "Abzug der Sondervorauszahlung (Dauerfristverlängerung)", advance_payment, "tax", "manual")
            deductible += advance_payment

    row(50, "83", "Verbleibende Vorauszahlung (+) / Überschuss (−)", owed - deductible, "tax", "result")
    return rows
