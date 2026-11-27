"""Products without a picture get one from eBay; an existing picture is never replaced."""

import uuid

import httpx

from app.database import SessionLocal
from app.models.ebay_account import EbayAccount
from app.models.product import Product
from app.services import ebay_listing_service, ebay_order_service

PICTURE = "https://i.ebayimg.com/images/g/abc/s-l140.jpg"

ORDERS = [{"orderId": "p1", "lineItems": [
    {"sku": "CUSTOM-1", "legacyItemId": "111", "title": "Adapter", "quantity": 1, "lineItemCost": {"value": "3"}},
    {"legacyItemId": "222", "title": "Kabel", "quantity": 1, "lineItemCost": {"value": "3"}}]}]


class _Orders:
    status_code = 200
    text = ""

    def __init__(self, orders):
        self.orders = orders

    def json(self):
        return {"orders": self.orders, "transactions": []}


def _setup(register, client, monkeypatch, orders):
    headers, _ = register()
    tenant_id = client.get("/auth/me", headers=headers).json()["tenant_id"]
    monkeypatch.setattr(ebay_order_service.ebay_oauth_service, "get_valid_access_token", lambda a, d: "t")
    monkeypatch.setattr(ebay_order_service.ebay_signing_service, "signed_headers", lambda *a, **k: {})
    monkeypatch.setattr(ebay_order_service.httpx, "get", lambda *a, **k: _Orders(orders))
    return tenant_id


def _account(db, tenant_id):
    account = EbayAccount(id=uuid.uuid4(), tenant_id=tenant_id, environment="PRODUCTION", status="CONNECTED")
    db.add(account)
    db.commit()
    return account


def _picture(db, tenant_id, sku):
    return db.query(Product).filter_by(tenant_id=tenant_id, sku=sku).one().image_url


def test_order_sync_fills_missing_pictures_only(register, client, monkeypatch):
    tenant_id = _setup(register, client, monkeypatch, ORDERS)
    asked = []

    def fake_picture(account, token, item_id):
        asked.append(item_id)
        return PICTURE

    monkeypatch.setattr(ebay_listing_service, "get_item_picture", fake_picture)
    with SessionLocal() as db:
        db.add(Product(tenant_id=tenant_id, sku="222", title="Kabel", stock_quantity=1,
                       image_url="https://own/pic.jpg"))
        account = _account(db, tenant_id)
        ebay_order_service.sync_orders(account, tenant_id, db)
        assert _picture(db, tenant_id, "CUSTOM-1") == PICTURE
        assert _picture(db, tenant_id, "222") == "https://own/pic.jpg"
        # A second sync (orders already known) has nothing left to look up.
        ebay_order_service.sync_orders(account, tenant_id, db)
    assert asked == ["111"]


def test_failing_picture_lookup_does_not_break_the_sync(register, client, monkeypatch):
    tenant_id = _setup(register, client, monkeypatch, ORDERS)

    def broken(*args):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(ebay_listing_service, "get_item_picture", broken)
    with SessionLocal() as db:
        result = ebay_order_service.sync_orders(_account(db, tenant_id), tenant_id, db)
    assert result["created"] == 1


def test_listing_import_fills_a_missing_picture(register, client, monkeypatch):
    tenant_id = _setup(register, client, monkeypatch, [])
    monkeypatch.setattr(ebay_listing_service, "fetch_active_listings", lambda a, d: [
        {"item_id": "333", "sku": None, "title": "DAC", "price": 2, "available": 24, "image_url": PICTURE}])
    with SessionLocal() as db:
        db.add(Product(tenant_id=tenant_id, sku="333", title="Mein Titel", stock_quantity=0))
        result = ebay_listing_service.import_listings(_account(db, tenant_id), tenant_id, db)
        product = db.query(Product).filter_by(tenant_id=tenant_id, sku="333").one()
        assert (product.image_url, product.title, product.stock_quantity) == (PICTURE, "Mein Titel", 0)
    assert result["products_created"] == 0


def test_get_item_picture_reads_the_gallery_url(monkeypatch):
    xml = ('<GetItemResponse xmlns="urn:ebay:apis:eBLBaseComponents"><Item><PictureDetails>'
           f"<GalleryURL>{PICTURE}</GalleryURL></PictureDetails></Item></GetItemResponse>")
    monkeypatch.setattr(ebay_listing_service.httpx, "post",
                        lambda *a, **k: type("R", (), {"status_code": 200, "text": xml})())
    account = EbayAccount(environment="PRODUCTION")
    assert ebay_listing_service.get_item_picture(account, "t", "111") == PICTURE
