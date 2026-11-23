"""Returns: refund comes off revenue, goods are a loss unless the item came back."""
from datetime import datetime, timezone
from decimal import Decimal

from app.database import SessionLocal
from app.models.order import Order
from app.services.ebay_order_service import _refund_total


def _order(client, headers):
    pid = client.post("/products", headers=headers, json={
        "sku": "R1", "title": "R1", "purchase_price": 1, "supplier_shipping_cost": 0.5,
        "packaging_cost": 0.5, "selling_price": 3, "stock_quantity": 10}).json()["id"]
    r = client.post("/orders", headers=headers, json={"items": [{"product_id": pid, "quantity": 1}]})
    assert r.status_code in (200, 201)
    return r.json()["id"]


def _totals(client, headers):
    return client.get("/accounting/summary", headers=headers,
                      params={"start": "2020-01-01", "end": "2029-12-31"}).json()["totals"]


def test_refund_lost_goods_by_default(client, register):
    headers, _ = register()
    oid = _order(client, headers)
    # sale 3, cost 2 -> profit 1; refund 3 and item kept by buyer -> -2
    r = client.patch(f"/orders/{oid}/refund", headers=headers, json={"refund_amount": 3})
    assert r.status_code == 200 and float(r.json()["total_profit"]) == -2
    t = _totals(client, headers)
    assert float(t["refunds"]) == 3 and t["orders_refunded"] == 1 and float(t["net_profit"]) == -2


def test_refund_item_returned_and_return_shipping(client, register):
    headers, _ = register()
    oid = _order(client, headers)
    r = client.patch(f"/orders/{oid}/refund", headers=headers,
                     json={"refund_amount": 3, "item_returned_to_stock": True, "return_shipping_cost": 1.5})
    assert float(r.json()["total_profit"]) == -1.5
    t = _totals(client, headers)
    assert float(t["goods_cost"]) == 0 and float(t["return_shipping"]) == 1.5 and float(t["net_profit"]) == -1.5


def test_refund_unknown_order_404(client, register):
    headers, _ = register()
    r = client.patch("/orders/00000000-0000-0000-0000-000000000000/refund", headers=headers, json={"refund_amount": 1})
    assert r.status_code == 404


def test_refund_total_from_ebay_payload():
    assert _refund_total({}) is None
    payload = {"paymentSummary": {"refunds": [{"amount": {"value": "2.50"}}, {"amount": {"value": "0.50"}}]}}
    assert _refund_total(payload) == Decimal("3.00")
