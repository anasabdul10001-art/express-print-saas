"""Accounting overview: revenue, fees, goods cost, expenses -> net profit per period."""
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models.order import Order


def _setup(client, headers):
    pid = client.post("/products", headers=headers, json={
        "sku": "A1", "title": "A1", "purchase_price": 1, "supplier_shipping_cost": 0.5,
        "packaging_cost": 0.5, "selling_price": 5, "stock_quantity": 50}).json()["id"]
    for _ in range(2):
        assert client.post("/orders", headers=headers, json={"items": [{"product_id": pid, "quantity": 2}]}).status_code in (200, 201)
    return pid


def _set_dates(tenant_id, dates, fees):
    with SessionLocal() as db:
        orders = db.query(Order).filter(Order.tenant_id == tenant_id).order_by(Order.created_at).all()
        for order, d, fee in zip(orders, dates, fees):
            order.ordered_at = d
            order.ebay_fees = fee
        db.commit()


def test_net_profit_and_grouping(client, register):
    headers, _ = register()
    tenant_id = client.get("/auth/me", headers=headers).json()["tenant_id"]
    _setup(client, headers)
    _set_dates(tenant_id, [datetime(2026, 11, 5, 12, tzinfo=timezone.utc), datetime(2026, 12, 2, 12, tzinfo=timezone.utc)],
               [1.5, 2.0])
    client.post("/expenses", headers=headers, json={"description": "Internet", "amount": 3, "expense_date": "2026-11-10", "category": "Internet"})

    r = client.get("/accounting/summary", headers=headers,
                   params={"start": "2026-11-01", "end": "2026-12-31", "group_by": "month"}).json()
    t = r["totals"]
    # revenue 2 orders * (2*5)=20, fees 3.5, goods 2*(2*2)=8, expenses 3 -> 5.5
    assert float(t["revenue"]) == 20 and float(t["ebay_fees"]) == 3.5
    assert float(t["goods_cost"]) == 8 and float(t["expenses"]) == 3
    assert float(t["net_profit"]) == 5.5 and t["orders"] == 2
    assert t["orders_missing_cost"] == 0 and t["orders_missing_fees"] == 0
    assert [s["period"] for s in r["series"]] == ["2026-11", "2026-12"]
    nov = r["series"][0]
    assert float(nov["net_profit"]) == 10 - 1.5 - 4 - 3
    assert r["expenses_by_category"] == [{"category": "Internet", "amount": "3.00"}]


def test_period_filter_and_missing_data_flags(client, register):
    headers, _ = register()
    tenant_id = client.get("/auth/me", headers=headers).json()["tenant_id"]
    _setup(client, headers)
    _set_dates(tenant_id, [datetime(2026, 11, 5, 12, tzinfo=timezone.utc), datetime(2026, 12, 2, 12, tzinfo=timezone.utc)],
               [None, 2.0])
    r = client.get("/accounting/summary", headers=headers, params={"start": "2026-11-01", "end": "2026-11-30"}).json()
    assert r["totals"]["orders"] == 1 and r["totals"]["orders_missing_fees"] == 1 and r["series"] == []


def test_validation_and_tenant_isolation(client, register):
    h1, _ = register()
    h2, _ = register()
    t1 = client.get("/auth/me", headers=h1).json()["tenant_id"]
    _setup(client, h1)
    _set_dates(t1, [datetime(2026, 11, 5, 12, tzinfo=timezone.utc)] * 2, [1, 1])
    other = client.get("/accounting/summary", headers=h2, params={"start": "2026-11-01", "end": "2026-11-30"}).json()
    assert other["totals"]["orders"] == 0
    bad = client.get("/accounting/summary", headers=h1, params={"start": "2026-12-01", "end": "2026-11-01"})
    assert bad.status_code == 400
