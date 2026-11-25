"""Figures for the Jobcenter's Anlage EKS."""
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from app.database import SessionLocal
from app.models.order import Order
from app.models.tenant import Tenant

HALF = {"start": "2026-07-01", "end": "2026-12-31"}


def _tenant(client, headers):
    return uuid.UUID(client.get("/auth/me", headers=headers).json()["tenant_id"])


def _fill(client, headers):
    tid = _tenant(client, headers)
    with SessionLocal() as db:
        db.add(Order(tenant_id=tid, external_order_ref="1", status="new", ebay_buyer_total=Decimal("100.00"),
                     ebay_fees=Decimal("10.00"), shipping_label_cost=Decimal("4.00"), refund_amount=Decimal("6.00"),
                     ordered_at=datetime(2026, 7, 5, 12, tzinfo=timezone.utc)))
        db.commit()
    pid = client.post("/products", headers=headers, json={"sku": "A", "title": "A", "selling_price": 5,
                                                          "stock_quantity": 0}).json()["id"]
    data = {"supplier": "L", "invoice_number": "1", "invoice_date": "2026-08-01", "paid_date": "2026-08-02",
            "lines": [{"product_id": pid, "quantity": 10, "line_total": "20.00"}]}
    assert client.post("/purchases", headers=headers, data={"data": json.dumps(data)},
                       files={"file": ("r.pdf", b"%PDF-1.4 x", "application/pdf")}).status_code == 201
    for category, amount in (("Werbung", 3), ("Internet & Telefon", 30), ("Strom", 12), ("Büro & Material", 2),
                             ("Verpackung", 7), ("=BOESE()", 1)):
        r = client.post("/expenses", headers=headers, json={"description": "x", "amount": amount,
                                                            "category": category, "expense_date": "2026-09-10"})
        assert r.status_code == 201, r.text


def _report(client, headers, **period):
    r = client.get("/accounting/eks", headers=headers, params=period or HALF)
    assert r.status_code == 200, r.text
    return r.json()


def _line(data, code):
    return next(r for r in data["rows"] if r["line"] == code)


def _named(data, name):
    return next(r for r in data["rows"] if r["name"] == name)


def test_amounts_land_in_the_forms_lines_by_month(client, register):
    headers, _ = register()
    _fill(client, headers)
    d = _report(client, headers)
    assert d["months"] == ["07/2026", "08/2026", "09/2026", "10/2026", "11/2026", "12/2026"]
    assert _line(d, "A1")["months"][0] == "100.00"
    assert _line(d, "B1")["months"][1] == "20.00"
    assert _line(d, "B3")["total"] == "12.00" and _line(d, "B5")["total"] == "3.00"
    assert _line(d, "B10")["total"] == "6.00"   # shipping label 4 + office material 2
    assert _line(d, "B11")["total"] == "30.00"
    assert _line(d, "B14.5")["total"] == "24.00"  # fees 10 + refund 6 + packaging 7 + own category 1
    assert _line(d, "A2")["months"] is None and _line(d, "B6.1")["total"] is None
    assert _named(d, "Summe der Betriebseinnahmen")["total"] == "100.00"
    assert _named(d, "Zwischensumme Tabelle B – Teil 1")["total"] == "35.00"
    assert _named(d, "Zwischensumme Tabelle B – Teil 2")["total"] == "41.00"
    assert _named(d, "Summe Betriebsausgaben")["total"] == "95.00"
    assert d["profit_total"] == "5.00" and d["profit_monthly_average"] == "0.83"
    assert {"line": "B14.5", "what": "Verpackung", "amount": "7.00"} in d["remarks"]
    text = " ".join(d["warnings"])
    assert "Kontoauszug" in text and "B11" in text and "B3" in text


def test_it_agrees_with_the_euer(client, register):
    headers, _ = register()
    _fill(client, headers)
    euer = client.get("/accounting/euer", headers=headers, params={**HALF, "group_by": "year"}).json()
    assert _report(client, headers)["profit_total"] == euer["total"]["surplus"]


def test_the_csv_has_the_lines_and_defuses_formulas(client, register):
    headers, _ = register()
    _fill(client, headers)
    r = client.get("/accounting/eks/table.csv", headers=headers, params=HALF)
    lines = r.content.decode("utf-8-sig").splitlines()
    assert any(l.startswith("A1;Betriebseinnahmen;100,00;0,00") for l in lines)
    assert any(l.startswith("B14.5;'=BOESE()") for l in lines)
    assert any(l.startswith("A2;Privatentnahmen von Waren;;") for l in lines)


def test_limits_vat_sellers_other_tenants_and_login(client, register):
    headers, _ = register()
    _fill(client, headers)
    assert client.get("/accounting/eks", headers=headers,
                      params={"start": "2025-12-01", "end": "2026-12-31"}).status_code == 400
    assert client.get("/accounting/eks", headers=headers,
                      params={"start": "2026-02-01", "end": "2026-01-01"}).status_code == 400
    other, _ = register()
    assert _report(client, other)["profit_total"] == "0.00"
    assert client.get("/accounting/eks", params=HALF).status_code in (401, 403, 422)
    with SessionLocal() as db:
        db.query(Tenant).filter(Tenant.id == _tenant(client, headers)).update({"vat_mode": "regular"})
        db.commit()
    r = client.get("/accounting/eks", headers=headers, params=HALF)
    assert r.status_code == 400 and "Umsatzsteuer" in r.json()["detail"]
