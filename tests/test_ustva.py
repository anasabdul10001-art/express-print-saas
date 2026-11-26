"""Filling help for the VAT advance return (form USt 1 A 2026)."""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.database import SessionLocal
from app.models.cash_event import CashEvent
from app.models.expense import Expense
from app.models.order import Order
from app.models.purchase import Purchase
from app.services import ustva_service as svc

D = Decimal


def _compute(regular, sales="0", sums=None, input_vat="0", advance="0"):
    rows = svc.compute(regular=regular, sales_gross=D(sales), sums={k: D(v) for k, v in (sums or {}).items()},
                       input_vat=D(input_vat), advance_payment=D(advance))
    return {r["kz"]: r["value"] for r in rows if r["kz"]}


def test_periods():
    assert svc.period_range("Q3") == (date(2026, 7, 1), date(2026, 9, 30))
    assert svc.period_range("Q4") == (date(2026, 10, 1), date(2026, 12, 31))
    assert svc.period_range("02") == (date(2026, 2, 1), date(2026, 2, 28))
    assert svc.period_range("12") == (date(2026, 12, 1), date(2026, 12, 31))


def test_regular_bases_in_whole_euros_and_balance():
    out = _compute(True, sales="100.00", sums={"eu_service": "10.00"}, input_vat="19.00")
    assert out == {"81": "84", "66": "19.00", "46": "10", "47": "1.90", "67": "1.90", "83": "-3.04"}


def test_regular_eu_goods_import_vat_and_advance():
    out = _compute(True, sales="1190.00", sums={"eu_goods": "200.99", "import_vat": "30.00", "other_13b": "50.00"},
                   advance="10.00")
    assert out["81"] == "1000" and out["89"] == "200" and out["61"] == "38.00" and out["62"] == "30.00"
    assert out["84"] == "50" and out["85"] == "9.50" and out["67"] == "9.50" and out["39"] == "10.00"
    # 190 + 38 + 9.50 - (0 + 38 + 30 + 9.50 + 10)
    assert out["83"] == "150.00"


def test_small_business_owes_reverse_charge_only_and_deducts_nothing():
    out = _compute(False, sales="500.00", sums={"eu_service": "10.50", "import_vat": "5.00"}, input_vat="19.00")
    assert out == {"46": "10", "47": "2.00", "83": "2.00"}


def _seed(tenant_id):
    with SessionLocal() as db:
        db.add(Expense(id=uuid.uuid4(), tenant_id=tenant_id, description="Hosting", supplier="Cloud",
                       amount=D("10.00"), expense_date=date(2026, 8, 5)))
        db.add(Expense(id=uuid.uuid4(), tenant_id=tenant_id, description="eBay Gebühr", amount=D("5.00"),
                       expense_date=date(2026, 8, 6), external_ref="fee-1"))
        db.add(Expense(id=uuid.uuid4(), tenant_id=tenant_id, description="anderes Quartal", amount=D("99.00"),
                       expense_date=date(2026, 6, 30)))
        db.add(Purchase(id=uuid.uuid4(), tenant_id=tenant_id, supplier="Händler", invoice_date=date(2026, 9, 30),
                        total=D("119.00"), extra_costs=D("0"), file_name="r.pdf", content_type="application/pdf",
                        file_data=b"%PDF-1.4"))
        db.commit()


def _setup(register, client, mode="regular"):
    headers, _ = register()
    tenant_id = client.get("/auth/me", headers=headers).json()["tenant_id"]
    client.put("/accounting/vat-settings", headers=headers, json={"vat_mode": mode})
    return headers, tenant_id


def _values(data):
    return {r["kz"]: r["value"] for r in data["rows"] if r["kz"]}


def _order(db, tenant_id, day, total, refund=None, country="DE", ref="o", status="shipped", payment=None):
    order = Order(id=uuid.uuid4(), tenant_id=tenant_id, external_order_ref=ref, status=status,
                  ordered_at=datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc),
                  ebay_buyer_total=D(total), refund_amount=D(refund) if refund else None,
                  recipient_country_code=country, ebay_payment_status=payment)
    db.add(order)
    db.flush()
    return order.id


def test_input_vat_comes_only_from_the_invoices(register, client):
    headers, tenant_id = _setup(register, client)
    _seed(tenant_id)
    first = client.get("/accounting/ustva?period=Q3", headers=headers).json()
    assert [i["kind"] for i in first["items"]] == ["expense", "purchase"]
    assert first["complete"] is False and _values(first)["66"] == "0.00"
    expense, purchase = first["items"]
    url = "/accounting/ustva/classification"
    client.put(url, headers=headers, json={"kind": "expense", "id": expense["id"], "tax_treatment": "eu_service"})
    client.put(url, headers=headers, json={"kind": "purchase", "id": purchase["id"], "tax_treatment": "de_vat"})
    middle = client.get("/accounting/ustva?period=Q3", headers=headers).json()
    # a German invoice without the typed VAT is not estimated
    assert _values(middle)["66"] == "0.00" and any("laut Rechnung" in w for w in middle["open_points"])
    too_much = {"kind": "purchase", "id": purchase["id"], "tax_treatment": "de_vat", "vat_amount": "20.00"}
    assert client.put(url, headers=headers, json=too_much).status_code == 422
    client.put(url, headers=headers, json={**too_much, "vat_amount": "17.35"})  # e.g. mixed rates
    # the imported eBay fee makes the figures from eBay's invoice necessary
    pending = client.get("/accounting/ustva?period=Q3", headers=headers).json()
    assert pending["complete"] is False and len(pending["open_points"]) == 2
    client.put("/accounting/ustva/period-inputs", headers=headers,
               json={"period": "Q3", "ebay_fees_tax": "de_vat", "ebay_amount": "0.80", "labels_vat": "0"})
    done = client.get("/accounting/ustva?period=Q3", headers=headers).json()
    assert done["complete"] is True, done["open_points"]
    values = _values(done)
    assert values["66"] == "18.15" and values["47"] == "1.90" and values["83"] == "-18.15"
    # the figures belong to their period only
    assert client.get("/accounting/ustva?period=Q4", headers=headers).json()["ebay"]["ebay_amount"] is None


def test_refunds_count_in_the_period_they_were_paid(register, client):
    headers, tenant_id = _setup(register, client)
    with SessionLocal() as db:
        old = _order(db, tenant_id, date(2026, 6, 20), "119.00", refund="119.00", ref="june")
        db.add(CashEvent(id=uuid.uuid4(), tenant_id=tenant_id, order_id=old, kind="refund", external_ref="t1",
                         amount=D("119.00"), booked_on=date(2026, 7, 3)))
        _order(db, tenant_id, date(2026, 8, 1), "238.00", ref="aug")
        _order(db, tenant_id, date(2026, 8, 2), "50.00", ref="unpaid", payment="PENDING")
        _order(db, tenant_id, date(2026, 8, 3), "70.00", ref="gone", status="canceled")
        db.commit()
    june = client.get("/accounting/ustva?period=Q2", headers=headers).json()
    assert june["sales"] == {"sold": "119.00", "refunded": "0", "net": "119.00"}
    summer = client.get("/accounting/ustva?period=Q3", headers=headers).json()
    assert summer["sales"] == {"sold": "238.00", "refunded": "119.00", "net": "119.00"}
    assert _values(summer)["81"] == "100" and summer["complete"] is True, summer["open_points"]


def test_refund_without_a_date_blocks_until_it_is_entered(register, client):
    headers, tenant_id = _setup(register, client)
    with SessionLocal() as db:
        order_id = str(_order(db, tenant_id, date(2026, 8, 1), "119.00", refund="23.80", ref="hand"))
        db.commit()
    before = client.get("/accounting/ustva?period=Q3", headers=headers).json()
    assert before["complete"] is False and before["sales"]["refunded"] == "0"
    assert before["manual_refunds"][0]["amount"] == "23.80"
    url = "/accounting/ustva/refund-date"
    assert client.put(url, headers=headers, json={"order_id": order_id, "refund_date": "2026-07-01"}).status_code == 422
    other, _ = register()
    assert client.put(url, headers=other, json={"order_id": order_id, "refund_date": "2026-10-05"}).status_code == 404
    client.put(url, headers=headers, json={"order_id": order_id, "refund_date": "2026-10-05"})
    q3 = client.get("/accounting/ustva?period=Q3", headers=headers).json()
    q4 = client.get("/accounting/ustva?period=Q4", headers=headers).json()
    assert q3["complete"] is True and q3["sales"]["net"] == "119.00"
    assert q4["sales"]["refunded"] == "23.80" and q4["sales"]["net"] == "-23.80" and _values(q4)["81"] == "-20"


def test_foreign_or_unknown_destination_is_an_open_point(register, client):
    headers, tenant_id = _setup(register, client)
    with SessionLocal() as db:
        _order(db, tenant_id, date(2026, 8, 1), "10.00", country="AT", ref="a")
        _order(db, tenant_id, date(2026, 8, 1), "10.00", country=None, ref="b")
        db.commit()
    data = client.get("/accounting/ustva?period=Q3", headers=headers).json()
    assert data["complete"] is False
    assert any("Ausland" in w for w in data["open_points"]) and any("Lieferland" in w for w in data["open_points"])


@pytest.mark.parametrize("mode", ["regular", "kleinunternehmer"])
def test_a_period_without_any_data_is_not_complete(register, client, mode):
    headers, tenant_id = _setup(register, client, mode)
    empty = client.get("/accounting/ustva?period=Q2", headers=headers).json()
    assert empty["complete"] is False and any("keine Daten" in w for w in empty["open_points"])
    # entering a zero or a prepayment does not turn nothing into data
    client.put("/accounting/ustva/period-inputs", headers=headers, json={"period": "Q2", "labels_vat": "0"})
    still = client.get("/accounting/ustva?period=Q2&advance_payment=10", headers=headers).json()
    assert still["complete"] is False
    with SessionLocal() as db:
        _order(db, tenant_id, date(2026, 5, 1), "11.90")
        db.commit()
    filled = client.get("/accounting/ustva?period=Q2", headers=headers).json()
    assert filled["complete"] is True, filled["open_points"]


def test_endpoint_rejects_foreign_entries_and_wrong_input(register, client):
    headers, _ = register()
    other, _ = register()
    tenant_id = client.get("/auth/me", headers=headers).json()["tenant_id"]
    _seed(tenant_id)
    item = client.get("/accounting/ustva?period=Q3", headers=headers).json()["items"][1]
    body = {"kind": "purchase", "id": item["id"], "tax_treatment": "de_vat"}
    assert client.put("/accounting/ustva/classification", headers=other, json=body).status_code == 404
    assert client.put("/accounting/ustva/classification", headers=headers,
                      json={**body, "tax_treatment": "eu_service"}).status_code == 422
    assert client.get("/accounting/ustva?period=Q5", headers=headers).status_code == 400
    assert client.get("/accounting/ustva?period=Q1&year=2025", headers=headers).status_code == 400
    assert client.get("/accounting/ustva?period=Q1").status_code == 401
