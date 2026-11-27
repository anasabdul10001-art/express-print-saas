"""The sync the open pages ask for: order of steps, quantities, throttle, no overlap."""

import uuid

import pytest

from app.database import SessionLocal
from app.models.ebay_account import EbayAccount
from app.models.product import Product
from app.models.shopify_account import ShopifyAccount
from app.services import ebay_listing_service, ebay_order_service, live_sync

LISTINGS = [
    {"item_id": "1", "sku": None, "title": "DAC", "price": 2, "available": 24, "image_url": None},
    {"item_id": "2", "sku": None, "title": "Neu", "price": 5, "available": 3, "image_url": None},
]


@pytest.fixture
def shop(register, client, monkeypatch):
    headers, _ = register()
    tenant_id = client.get("/auth/me", headers=headers).json()["tenant_id"]
    steps = []

    def orders(account, tenant, db):
        steps.append("orders")
        return {"created": 1}

    def listings(account, db):
        steps.append("listings")
        return LISTINGS

    monkeypatch.setattr(ebay_order_service, "sync_orders", orders)
    monkeypatch.setattr(ebay_listing_service, "fetch_active_listings", listings)
    with SessionLocal() as db:
        db.add(EbayAccount(id=uuid.uuid4(), tenant_id=tenant_id, environment="PRODUCTION", status="CONNECTED"))
        db.add(Product(tenant_id=tenant_id, sku="1", title="DAC", stock_quantity=0))
        db.commit()
    return headers, tenant_id, steps


def _stock(tenant_id):
    with SessionLocal() as db:
        return {p.sku: p.stock_quantity for p in db.query(Product).filter_by(tenant_id=tenant_id)}


def test_reads_orders_first_then_adds_products_and_quantities(shop, client):
    headers, tenant_id, steps = shop
    result = client.post("/ebay/live-sync", headers=headers).json()
    assert result["ran"] and result["errors"] == []
    assert (result["new_orders"], result["new_products"], result["quantities_updated"]) == (1, 1, 1)
    assert steps == ["orders", "listings"]
    assert _stock(tenant_id) == {"1": 24, "2": 3}


def test_a_second_call_right_away_does_not_ask_ebay_again(shop, client):
    headers, _, steps = shop
    client.post("/ebay/live-sync", headers=headers)
    assert client.post("/ebay/live-sync", headers=headers).json()["ran"] is False
    assert steps == ["orders", "listings"]


def test_does_not_overlap_a_running_import(shop, client):
    headers, tenant_id, steps = shop
    with live_sync.lock_for(tenant_id):
        assert client.post("/ebay/live-sync", headers=headers).json()["ran"] is False
    assert steps == []


def test_quantities_stay_when_shopify_is_connected_too(shop, client):
    headers, tenant_id, _ = shop
    with SessionLocal() as db:
        db.add(ShopifyAccount(tenant_id=tenant_id, shop_domain=f"{uuid.uuid4().hex}.myshopify.com",
                              status="CONNECTED"))
        db.commit()
    assert client.post("/ebay/live-sync", headers=headers).json()["quantities_updated"] == 0
    assert _stock(tenant_id)["1"] == 0


def test_an_ebay_error_changes_no_quantity(shop, client, monkeypatch):
    headers, tenant_id, _ = shop

    def broken(account, db):
        raise ValueError("eBay down")

    monkeypatch.setattr(ebay_listing_service, "fetch_active_listings", broken)
    result = client.post("/ebay/live-sync", headers=headers).json()
    assert result["errors"] and result["quantities_updated"] == 0
    assert _stock(tenant_id) == {"1": 0}


def test_needs_login(client):
    assert client.post("/ebay/live-sync").status_code in (401, 403)
