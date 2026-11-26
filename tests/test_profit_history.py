"""Dashboard chart: free date ranges, totals per day or per month."""
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.database import SessionLocal
from app.models.order import Order

BERLIN = ZoneInfo("Europe/Berlin")


def _sale(client, headers, pid, when):
    oid = client.post("/orders", headers=headers, json={"items": [{"product_id": pid, "quantity": 1}]}).json()["id"]
    with SessionLocal() as db:
        db.query(Order).filter(Order.id == oid).one().ordered_at = when
        db.commit()


def _product(client, headers):
    return client.post("/products", headers=headers, json={
        "sku": "C1", "title": "C1", "purchase_price": 4, "selling_price": 10, "stock_quantity": 20}).json()["id"]


def _history(client, headers, **params):
    response = client.get("/orders/profit-history", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_range_and_month_grouping(client, register):
    headers, _ = register()
    pid = _product(client, headers)
    for day in ("2026-01-10", "2026-01-20", "2026-03-05", "2026-03-25"):
        _sale(client, headers, pid, datetime.fromisoformat(day + "T12:00:00").replace(tzinfo=BERLIN))

    daily = _history(client, headers, start="2026-01-10", end="2026-01-12")
    assert [p["date"] for p in daily] == ["2026-01-10", "2026-01-11", "2026-01-12"]
    assert [p["orders"] for p in daily] == [1, 0, 0]
    assert Decimal(daily[0]["revenue"]) == 10 and Decimal(daily[0]["profit"]) == 6

    # a sale after the end date (25.03.) and one before the start (10.01.) are left out
    monthly = _history(client, headers, start="2026-01-15", end="2026-03-10", group="month")
    assert [p["date"] for p in monthly] == ["2026-01-01", "2026-02-01", "2026-03-01"]
    assert [p["orders"] for p in monthly] == [1, 0, 1]
    assert [Decimal(p["revenue"]) for p in monthly] == [10, 0, 10]


def test_long_ranges_and_bad_input(client, register):
    headers, _ = register()
    assert len(_history(client, headers, days=365)) == 365
    today = datetime.now(BERLIN).date()
    # an end date in the future stops at today
    assert _history(client, headers, start=str(today), end=str(today + timedelta(days=30)))[-1]["date"] == str(today)
    get = lambda **p: client.get("/orders/profit-history", headers=headers, params=p).status_code
    assert get(start="2026-02-01", end="2026-01-01") == 400
    assert get(start="1990-01-01") == 400
    assert get(group="week") == 422
