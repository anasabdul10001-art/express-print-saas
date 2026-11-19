"""eBay order sync: unknown products are created from the order line instead of hiding the sale."""

import uuid

import pytest

from app.database import SessionLocal
from app.models.ebay_account import EbayAccount
from app.models.order import Order
from app.models.product import Product
from app.services import ebay_order_service


class _Resp:
    status_code = 200
    text = ""

    def __init__(self, orders):
        self._orders = orders

    def json(self):
        return {"orders": self._orders}


def _sync(register, client, monkeypatch, orders):
    headers, _ = register()
    tenant_id = client.get("/auth/me", headers=headers).json()["tenant_id"]
    with SessionLocal() as db:
        account = EbayAccount(id=uuid.uuid4(), tenant_id=tenant_id, environment="PRODUCTION", status="CONNECTED")
        db.add(account)
        db.commit()
        monkeypatch.setattr(ebay_order_service.ebay_oauth_service, "get_valid_access_token", lambda a, d: "t")
        monkeypatch.setattr(ebay_order_service.httpx, "get", lambda *a, **k: _Resp(orders))
        result = ebay_order_service.sync_orders(account, tenant_id, db)
    return tenant_id, result


def test_unknown_sku_creates_product_and_imports_order(register, client, monkeypatch):
    orders = [{"orderId": "o1", "buyer": {"username": "b"}, "lineItems": [
        {"sku": "NEW-1", "title": "USB-C Kabel", "quantity": 2, "lineItemCost": {"value": "7.18", "currency": "EUR"}}]}]
    tenant_id, result = _sync(register, client, monkeypatch, orders)
    assert result["created"] == 1 and result["products_created"] == 1 and result["skipped"] == 0
    with SessionLocal() as db:
        product = db.query(Product).filter_by(tenant_id=tenant_id, sku="NEW-1").one()
        assert product.title == "USB-C Kabel" and float(product.selling_price) == 3.59
        assert product.purchase_price is None and product.stock_quantity == 0
        assert db.query(Order).filter_by(external_order_ref="o1").one()


def test_line_without_sku_falls_back_to_item_number(register, client, monkeypatch):
    orders = [{"orderId": "o2", "lineItems": [
        {"legacyItemId": "920105301438", "title": "Adapter", "quantity": 1, "lineItemCost": {"value": "3.59"}}]}]
    tenant_id, result = _sync(register, client, monkeypatch, orders)
    assert result["created"] == 1
    with SessionLocal() as db:
        assert db.query(Product).filter_by(tenant_id=tenant_id, sku="920105301438").one()


def test_line_with_neither_sku_nor_item_number_is_skipped(register, client, monkeypatch):
    orders = [{"orderId": "o3", "lineItems": [{"title": "Mystery", "quantity": 1}]}]
    _, result = _sync(register, client, monkeypatch, orders)
    assert result["created"] == 0 and result["skipped"] == 1


def test_sync_stores_ebay_fees_and_profit_is_net(register, client, monkeypatch):
    orders = [{"orderId": "o9", "lineItems": [
        {"sku": "F-1", "title": "Kabel", "quantity": 1, "lineItemCost": {"value": "10.00"}}]}]
    headers, _ = register()
    tenant_id = client.get("/auth/me", headers=headers).json()["tenant_id"]
    with SessionLocal() as db:
        db.add(Product(tenant_id=tenant_id, sku="F-1", title="Kabel", selling_price=10, purchase_price=4, stock_quantity=1))
        account = EbayAccount(id=uuid.uuid4(), tenant_id=tenant_id, environment="PRODUCTION", status="CONNECTED")
        db.add(account)
        db.commit()
        monkeypatch.setattr(ebay_order_service.ebay_oauth_service, "get_valid_access_token", lambda a, d: "t")

        def fake_get(url, **k):
            if "finances" in url:
                r = _Resp([])
                r.json = lambda: {"transactions": [{"orderId": "o9", "totalFeeAmount": {"value": "1.50"}}]}
                return r
            return _Resp(orders)

        monkeypatch.setattr(ebay_order_service.httpx, "get", fake_get)
        result = ebay_order_service.sync_orders(account, tenant_id, db)
    assert result["fees_updated"] == 1
    out = client.get("/orders", headers=headers).json()[0]
    assert float(out["ebay_fees"]) == 1.5 and float(out["total_profit"]) == 4.5
