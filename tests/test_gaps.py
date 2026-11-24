"""Returns put stock back; dashboard figures follow the accounting rules; variation quantities."""
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


def test_variation_quantities_add_sold_back(monkeypatch):
    sent = []
    monkeypatch.setattr(ebay_listing_service.ebay_oauth_service, "get_valid_access_token", lambda a, d: "tok")
    monkeypatch.setattr(ebay_listing_service, "get_item_variations", lambda a, d, i: {"variations": [
        {"sku": None, "specifics": {"Farbe": "Rot"}, "sold": "2", "quantity": "3"},
        {"sku": "S-2", "specifics": {}, "sold": "0", "quantity": "5"},
    ]})

    class R:
        status_code = 200
        text = '<r xmlns="urn:ebay:apis:eBLBaseComponents"><Ack>Success</Ack></r>'

    monkeypatch.setattr(ebay_listing_service.httpx, "post", lambda url, **kw: sent.append(kw["content"]) or R())
    acct = EbayAccount(ebay_user_id="me", environment="PRODUCTION")
    ebay_listing_service.revise_variation_quantities(acct, None, "123", [4, 7])
    xml = sent[0]
    assert "<Value>Rot</Value></NameValueList></VariationSpecifics><Quantity>6</Quantity>" in xml  # 4 available + 2 sold
    assert "<SKU>S-2</SKU><Quantity>7</Quantity>" in xml


def test_variation_quantities_endpoint_sets_stock_sum(client, register):
    headers, _ = register()
    pid = client.post("/products", headers=headers, json={"sku": "920105301438", "title": "x", "selling_price": 3, "stock_quantity": 6}).json()["id"]
    with patch("app.routers.ebay._connected_accounts") as acc, \
         patch("app.routers.ebay.ebay_listing_service.revise_variation_quantities") as revise:
        acc.return_value.all.return_value = [EbayAccount(ebay_user_id="me", environment="PRODUCTION")]
        r = client.post("/ebay/variation-quantities", headers=headers, json={"product_id": pid, "available": [2, 2, 1, 4]})
    assert r.status_code == 200 and r.json()["stock_quantity"] == 9
    revise.assert_called_once()
    assert _stock(client, headers, pid) == 9
