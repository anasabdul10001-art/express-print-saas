"""EÜR: every amount counts when the money moved."""
import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from app.database import SessionLocal
from app.models.cash_event import CashEvent
from app.models.order import Order
from app.services import ebay_order_service

Q4 = {"start": "2026-10-01", "end": "2026-12-31"}
YEARS = {"start": "2026-01-01", "end": "2027-12-31"}


def _tenant(client, headers):
    return uuid.UUID(client.get("/auth/me", headers=headers).json()["tenant_id"])


def _order(tenant_id, ref="1-1", total="10.00", when=datetime(2026, 12, 20, 12, tzinfo=timezone.utc), **extra):
    with SessionLocal() as db:
        db.add(Order(tenant_id=tenant_id, external_order_ref=ref, status=extra.pop("status", "new"),
                     ebay_buyer_total=Decimal(total), ordered_at=when, **extra))
        db.commit()


def _tx(kind, ref, day, **extra):
    return {"orderId": "1-1", "transactionId": f"{kind}-{ref}", "transactionDate": f"{day}T10:00:00.000Z", **extra}


def _sync(tenant_id, transactions):
    with SessionLocal() as db, patch.object(
        ebay_order_service, "_fetch_transactions", side_effect=lambda a, d, t, kind, n: transactions.get(kind, [])
    ):
        ebay_order_service.sync_fees(object(), tenant_id, db, "tok", [])


def _report(client, headers, group_by="year", **period):
    r = client.get("/accounting/euer", headers=headers, params={**(period or YEARS), "group_by": group_by})
    assert r.status_code == 200, r.text
    return r.json()


REAL = {
    "SALE": [_tx("S", 1, "2026-12-20", totalFeeAmount={"value": "1.50"})],
    "REFUND": [_tx("R", 1, "2027-01-15", bookingEntry="DEBIT", amount={"value": "8.50"},
                   totalFeeAmount={"value": "1.50"})],
    "SHIPPING_LABEL": [_tx("L", 1, "2027-01-02", bookingEntry="DEBIT", amount={"value": "1.20"})],
}


def test_a_refund_counts_in_the_year_it_was_paid_not_the_year_of_the_sale(client, register):
    headers, _ = register()
    tid = _tenant(client, headers)
    _order(tid)
    _sync(tid, REAL)
    d = _report(client, headers)
    y2026, y2027 = d["periods"]
    assert (y2026["sales"], y2026["sale_fees"], y2026["refunds"], y2026["surplus"]) == ("10.00", "1.50", "0.00", "8.50")
    assert (y2027["sales"], y2027["refunds"], y2027["fee_credits"], y2027["labels"]) == ("0.00", "10.00", "1.50", "1.20")
    assert y2027["surplus"] == "-9.70" and d["total"]["surplus"] == "-1.20"
    assert d["checks"]["orders_with_estimated_dates"] == 0
    # only the later year: the sale itself is outside, its refund is not
    only = _report(client, headers, start="2027-01-01", end="2027-12-31")
    assert only["total"]["refunds"] == "10.00" and only["total"]["sales"] == "0.00"


def test_syncing_again_does_not_count_anything_twice(client, register):
    headers, _ = register()
    tid = _tenant(client, headers)
    _order(tid)
    _sync(tid, REAL)
    _sync(tid, REAL)
    with SessionLocal() as db:
        assert db.query(CashEvent).filter(CashEvent.tenant_id == tid).count() == 4
    assert _report(client, headers)["total"]["surplus"] == "-1.20"


def test_without_transaction_dates_the_order_date_is_used_and_reported(client, register):
    headers, _ = register()
    tid = _tenant(client, headers)
    _order(tid, ebay_fees=Decimal("1.00"), refund_amount=Decimal("2.00"), shipping_label_cost=Decimal("1.10"),
           return_shipping_cost=Decimal("3.00"))
    d = _report(client, headers, **Q4)
    assert d["total"]["surplus"] == "2.90"
    assert d["checks"]["orders_with_estimated_dates"] == 1
    assert any("Bestelldatum" in w for w in d["warnings"])


def test_a_hand_changed_amount_is_still_counted_in_full(client, register):
    headers, _ = register()
    tid = _tenant(client, headers)
    _order(tid)
    _sync(tid, REAL)
    with SessionLocal() as db:
        db.query(Order).filter(Order.tenant_id == tid).update({"refund_amount": Decimal("7.00")})
        db.commit()
    d = _report(client, headers)
    assert d["total"]["refunds"] == "7.00"
    assert d["checks"]["orders_with_estimated_dates"] == 1


def test_canceled_and_unpaid_orders_bring_no_income(client, register):
    headers, _ = register()
    tid = _tenant(client, headers)
    _order(tid, "A", status="canceled", refund_amount=Decimal("10.00"), shipping_label_cost=Decimal("1.00"))
    _order(tid, "B", ebay_payment_status="PENDING")
    d = _report(client, headers)
    assert (d["total"]["sales"], d["total"]["refunds"], d["total"]["labels"]) == ("0.00", "0.00", "1.00")


def test_goods_count_when_the_supplier_was_paid_not_when_sold(client, register):
    headers, _ = register()
    pid = client.post("/products", headers=headers, json={"sku": "A", "title": "A", "selling_price": 5,
                      "stock_quantity": 5, "purchase_price": 2}).json()["id"]
    client.post("/orders", headers=headers, json={"items": [{"product_id": pid, "quantity": 1}]})

    def buy(number, invoice_date, paid_date):
        data = {"supplier": "L", "invoice_number": number, "invoice_date": invoice_date, "paid_date": paid_date,
                "extra_costs": "4.00", "lines": [{"product_id": pid, "quantity": 10, "line_total": "20.00"}]}
        r = client.post("/purchases", headers=headers, data={"data": json.dumps(data)},
                        files={"file": ("r.pdf", b"%PDF-1.4 x", "application/pdf")})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    buy("1", "2026-03-30", "2026-04-02")
    buy("2", "2026-05-05", None)
    wrong = buy("3", "2026-05-06", "2026-05-06")
    assert client.post(f"/purchases/{wrong}/cancel", headers=headers).status_code == 200

    d = _report(client, headers, "quarter", start="2026-01-01", end="2026-06-30")
    assert [(p["label"], p["goods"]) for p in d["periods"]] == [("Q2/2026", "48.00")]
    assert d["checks"]["purchases_without_paid_date"] == 1
    everything = _report(client, headers, start="2026-01-01", end=date.today().replace(month=12, day=31).isoformat())
    assert everything["total"]["goods"] == "48.00"  # the sold piece's cost is not counted again


def test_expenses_with_hints_for_assets_and_the_turn_of_the_year(client, register):
    headers, _ = register()
    for description, amount, day in (("Laptop", 1200, "2026-06-01"), ("Internet", 30, "2026-12-30")):
        r = client.post("/expenses", headers=headers,
                        json={"description": description, "amount": amount, "expense_date": day})
        assert r.status_code == 201, r.text
    d = _report(client, headers, "month", start="2026-01-01", end="2026-12-31")
    assert d["total"]["expenses"] == "1230.00" and [p["label"] for p in d["periods"]] == ["06/2026", "12/2026"]
    assert d["checks"]["turn_of_year_expenses"] == 1 and len(d["checks"]["large_expenses"]) == 1


def test_the_csv_lists_every_booking_and_defuses_formulas(client, register):
    headers, _ = register()
    client.post("/expenses", headers=headers, json={"description": "x", "amount": 3, "expense_date": "2026-06-01",
                                                    "category": "=HYPERLINK(1)"})
    tid = _tenant(client, headers)
    _order(tid, "=1+1")
    r = client.get("/accounting/euer/bookings.csv", headers=headers, params=YEARS)
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"]
    lines = r.content.decode("utf-8-sig").strip().splitlines()
    assert len(lines) == 3
    assert lines[1].startswith("01.06.2026;Ausgabe;Sonstige Ausgaben;3,00;'=HYPERLINK(1)")
    assert lines[2].endswith("'=1+1")


def test_other_tenants_see_nothing_and_login_is_needed(client, register):
    first, _ = register()
    _order(_tenant(client, first))
    other, _ = register()
    assert _report(client, other)["periods"] == []
    assert client.get("/accounting/euer", params=YEARS).status_code in (401, 403, 422)
    bad = {"start": "2026-02-01", "end": "2026-01-01"}
    assert client.get("/accounting/euer", headers=first, params=bad).status_code == 400
