"""A quantity changed in ShipSync reaches the eBay listing. eBay is faked."""

import uuid

import pytest

from app.database import SessionLocal
from app.models.ebay_account import EbayAccount
from app.models.product import Product
from app.models.shopify_account import ShopifyAccount
from app.services import ebay_listing_service


@pytest.fixture
def shop(register, client, monkeypatch):
    headers, _ = register()
    tenant_id = client.get("/auth/me", headers=headers).json()["tenant_id"]
    ebay = {"available": {"111": 10}, "sent": [], "error": None}

    def listings(account, db):
        return [{"item_id": item, "sku": None, "title": item, "price": 2, "available": n, "image_url": None}
                for item, n in ebay["available"].items()]

    def revise(account, db, items):
        ebay["sent"] += [(item, quantity) for item, _, quantity in items]
        if not ebay["error"]:
            ebay["available"].update({item: quantity for item, _, quantity in items})
        return {item: ebay["error"] for item, _, _ in items}

    monkeypatch.setattr(ebay_listing_service, "fetch_active_listings", listings)
    monkeypatch.setattr(ebay_listing_service, "revise_listings", revise)
    with SessionLocal() as db:
        db.add(EbayAccount(id=uuid.uuid4(), tenant_id=tenant_id, environment="PRODUCTION", status="CONNECTED"))
        listed = Product(tenant_id=tenant_id, sku="111", title="Kabel", stock_quantity=10, selling_price=3)
        local = Product(tenant_id=tenant_id, sku="lokal", title="Nur hier", stock_quantity=4, selling_price=3)
        db.add_all([listed, local])
        db.commit()
        ids = {"listed": str(listed.id), "local": str(local.id)}
    return headers, tenant_id, ids, ebay


def _stock(product_id):
    with SessionLocal() as db:
        return db.query(Product).filter(Product.id == uuid.UUID(product_id)).one().stock_quantity


def test_a_typed_quantity_is_sent(shop, client):
    headers, _, ids, ebay = shop
    body = client.patch(f"/products/{ids['listed']}", headers=headers,
                        json={"stock_quantity": 7, "stock_seen": 10}).json()
    assert ebay["sent"] == [("111", 7)]
    assert body["stock_quantity"] == 7 and body["ebay_quantity_results"][0]["status"] == "ok"


def test_an_untouched_field_neither_overwrites_nor_sends(shop, client):
    headers, _, ids, ebay = shop
    # the form was opened at 9; meanwhile the sync stored 10
    body = client.patch(f"/products/{ids['listed']}", headers=headers,
                        json={"title": "Neu", "stock_quantity": 9, "stock_seen": 9}).json()
    assert ebay["sent"] == [] and body["stock_quantity"] == 10


def test_an_old_page_without_stock_seen_sends_nothing(shop, client):
    headers, _, ids, ebay = shop
    assert client.patch(f"/products/{ids['listed']}", headers=headers, json={"stock_quantity": 2}).status_code == 200
    assert ebay["sent"] == []


def test_restock_adds_to_what_ebay_has_now(shop, client):
    headers, _, ids, ebay = shop
    ebay["available"]["111"] = 8  # two sold, not yet read by ShipSync
    body = client.post(f"/products/{ids['listed']}/restock", headers=headers, json={"quantity": 5}).json()
    assert ebay["sent"] == [("111", 13)] and body["stock_quantity"] == 13


def test_own_order_and_return_change_the_listing(shop, client):
    headers, _, ids, ebay = shop
    order = client.post("/orders", headers=headers,
                        json={"items": [{"product_id": ids["listed"], "quantity": 3}]}).json()
    assert ebay["sent"] == [("111", 7)] and _stock(ids["listed"]) == 7
    client.patch(f"/orders/{order['id']}/refund", headers=headers, json={"item_returned_to_stock": True})
    assert ebay["sent"][-1] == ("111", 10) and _stock(ids["listed"]) == 10
    client.patch(f"/orders/{order['id']}/refund", headers=headers, json={"refund_amount": 1})
    assert len(ebay["sent"]) == 2  # no stock change, nothing sent


def test_a_product_without_listing_stays_local(shop, client):
    headers, _, ids, ebay = shop
    body = client.post(f"/products/{ids['local']}/restock", headers=headers, json={"quantity": 1}).json()
    assert ebay["sent"] == [] and body["stock_quantity"] == 5
    assert body["ebay_quantity_results"][0]["status"] == "no_listing"


def test_a_refusal_by_ebay_is_reported_and_the_change_stays_saved(shop, client):
    headers, _, ids, ebay = shop
    ebay["error"] = "abgelehnt"
    response = client.post(f"/products/{ids['listed']}/restock", headers=headers, json={"quantity": 1})
    assert response.status_code == 200
    result = response.json()["ebay_quantity_results"][0]
    assert result["status"] == "failed" and result["error"] == "abgelehnt"
    assert _stock(ids["listed"]) == 11


def test_ebay_unreachable_never_breaks_the_save(shop, client, monkeypatch):
    headers, _, ids, _ = shop

    def broken(account, db):
        raise ValueError("eBay down")

    monkeypatch.setattr(ebay_listing_service, "fetch_active_listings", broken)
    body = client.post(f"/products/{ids['listed']}/restock", headers=headers, json={"quantity": 1}).json()
    assert body["stock_quantity"] == 11 and body["ebay_quantity_results"][0]["status"] == "failed"


def test_with_shopify_too_shipsync_keeps_its_own_number(shop, client):
    headers, tenant_id, ids, ebay = shop
    ebay["available"]["111"] = 6
    with SessionLocal() as db:
        db.add(ShopifyAccount(tenant_id=tenant_id, shop_domain=f"{uuid.uuid4().hex}.myshopify.com",
                              status="CONNECTED"))
        db.commit()
    client.post(f"/products/{ids['listed']}/restock", headers=headers, json={"quantity": 2})
    assert ebay["sent"] == [("111", 8)] and _stock(ids["listed"]) == 12
