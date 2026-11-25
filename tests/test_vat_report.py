"""VAT advance-return preparation (USt-Voranmeldung) for regular 19% taxpayers."""
from datetime import datetime, timezone
from decimal import Decimal

from app.database import SessionLocal
from app.models.order import Order


def _regular_with_sales(client, register):
    headers, _ = register()
    tenant_id = client.get("/auth/me", headers=headers).json()["tenant_id"]
    client.put("/accounting/vat-settings", headers=headers, json={"vat_mode": "regular"})
    pid = client.post("/products", headers=headers, json={
        "sku": "V1", "title": "V1", "purchase_price": 1, "selling_price": 11.9, "stock_quantity": 9}).json()["id"]
    for _ in range(2):
        assert client.post("/orders", headers=headers, json={"items": [{"product_id": pid, "quantity": 1}]}).status_code in (200, 201)
    dates = [datetime(2026, 10, 20, 12, tzinfo=timezone.utc), datetime(2026, 11, 5, 12, tzinfo=timezone.utc)]
    with SessionLocal() as db:
        orders = db.query(Order).filter(Order.tenant_id == tenant_id).order_by(Order.created_at).all()
        for order, d in zip(orders, dates):
            order.ordered_at = d
            order.ebay_fees = Decimal("1.19")
        db.commit()
    return headers


def test_monthly_report_numbers(client, register):
    headers = _regular_with_sales(client, register)
    r = client.get("/accounting/vat-report", headers=headers, params={"start": "2026-10-01", "end": "2026-11-30"}).json()
    assert [x["period"] for x in r["rows"]] == ["10/2026", "11/2026"]
    row = r["rows"][0]
    # gross 11.90 -> VAT 1.90, net 10.00; input VAT: goods 1.00 -> 0.16, eBay fee 1.19 -> 0.19
    assert row["gross_sales_after_refunds"] == "11.90" and row["net_sales"] == "10.00" and row["sales_vat"] == "1.90"
    assert row["input_vat_parts"]["goods"] == "0.16" and row["input_vat_parts"]["ebay_fees"] == "0.19"
    assert row["input_vat"] == "0.35" and row["payable"] == "1.55"
    assert r["total"]["sales_vat"] == "3.80" and r["total"]["payable"] == "3.10"


def test_fees_can_be_excluded_and_quarter_and_year(client, register):
    headers = _regular_with_sales(client, register)
    p = {"start": "2026-10-01", "end": "2026-12-31", "fees_include_vat": "false"}
    r = client.get("/accounting/vat-report", headers=headers, params={**p, "group_by": "quarter"}).json()
    assert [x["period"] for x in r["rows"]] == ["Q4/2026"]
    assert r["rows"][0]["input_vat_parts"]["ebay_fees"] == "0.00" and r["rows"][0]["sales_vat"] == "3.80"
    r = client.get("/accounting/vat-report", headers=headers, params={**p, "group_by": "year"}).json()
    assert [x["period"] for x in r["rows"]] == ["2026"]


def test_csv_download(client, register):
    headers = _regular_with_sales(client, register)
    resp = client.get("/accounting/vat-report", headers=headers,
                      params={"start": "2026-10-01", "end": "2026-11-30", "output": "csv"})
    assert resp.status_code == 200 and "text/csv" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]
    lines = resp.content.decode("utf-8-sig").splitlines()
    assert lines[0].startswith("Zeitraum;Brutto-Umsatz") and lines[1].startswith("10/2026;11,90;10,00;1,90;")
    assert lines[3].startswith("Summe;") and "Vorbereitung" in lines[-1]


def test_only_for_regular_taxpayers(client, register):
    headers, _ = register()
    r = client.get("/accounting/vat-report", headers=headers, params={"start": "2026-10-01", "end": "2026-11-30"})
    assert r.status_code == 400
