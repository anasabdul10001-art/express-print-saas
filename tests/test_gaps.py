"""Returns put stock back; dashboard figures follow the accounting rules; variation quantities (see test_ebay_listing_editor)."""
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from app.database import SessionLocal
from app.models.ebay_account import EbayAccount
from app.models.order import Order
from app.services import ebay_listing_service


def _product_and_order(client, headers, stock=10, qty=2):
    pid = client.post("/products", headers=headers, json={
        "sku": "G1", "title": "G1", "purchase_price": 1, "supplier_shipping_cost": 0.5,
        "packaging_cost": 0.5, "selling_price": 3, "stock_quantity": stock}).json()["id"]
    oid = client.post("/orders", headers=headers, json={"items": [{"product_id": pid, "quantity": qty}]}).json()["id"]
    return pid, oid


def _stock(client, headers, pid):
    return next(p["stock_quantity"] for p in client.get("/products", headers=headers).json() if p["id"] == pid)


def test_marking_item_returned_restocks_and_unmarking_reverses(client, register):
    headers, _ = register()
    pid, oid = _product_and_order(client, headers)
    assert _stock(client, headers, pid) == 8
    client.patch(f"/orders/{oid}/refund", headers=headers, json={"refund_amount": 6, "item_returned_to_stock": True})
    assert _stock(client, headers, pid) == 10
    # saving again without changing the checkbox must not add stock twice
    client.patch(f"/orders/{oid}/refund", headers=headers, json={"refund_amount": 6, "item_returned_to_stock": True})
    assert _stock(client, headers, pid) == 10
    client.patch(f"/orders/{oid}/refund", headers=headers, json={"item_returned_to_stock": False})
    assert _stock(client, headers, pid) == 8


def test_dashboard_profit_includes_fees_and_refunds(client, register):
    headers, _ = register()
    pid, oid = _product_and_order(client, headers, qty=1)
    with SessionLocal() as db:
        order = db.query(Order).filter(Order.id == oid).one()
        order.ordered_at = datetime.now(timezone.utc)
        order.ebay_fees = Decimal("0.50")
        db.commit()
    s = client.get("/orders/summary", headers=headers).json()
    # revenue 3 - fees 0.5 - goods 2 = 0.5
    assert s["orders_today"] == 1 and float(s["profit_today"]) == 0.5
    client.patch(f"/orders/{oid}/refund", headers=headers, json={"refund_amount": 3})
    s = client.get("/orders/summary", headers=headers).json()
    assert float(s["profit_month"]) == -2.5  # 3 - 3 - 0.5 - 2
    hist = client.get("/orders/profit-history", headers=headers, params={"days": 3}).json()
    assert float(hist[-1]["profit"]) == -2.5
