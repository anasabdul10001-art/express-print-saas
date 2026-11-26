"""Taking eBay's available quantities over into ShipSync (never the other way)."""

import uuid

from app.database import SessionLocal
from app.models.ebay_account import EbayAccount
from app.models.product import Product
from app.models.stock_movement import StockMovement
from app.routers import ebay as ebay_router
from app.services import ebay_listing_service


def _listing(item_id, available, sku=None):
    return {"item_id": item_id, "sku": sku, "title": item_id, "price": None,
            "available": available, "image_url": None}


def _setup(register, client, monkeypatch, listings, products, new_orders=0):
    headers, _ = register()
    tenant_id = client.get("/auth/me", headers=headers).json()["tenant_id"]
    ids = {}
    with SessionLocal() as db:
        db.add(EbayAccount(id=uuid.uuid4(), tenant_id=tenant_id, environment="PRODUCTION", status="CONNECTED"))
        for sku, fields in products.items():
            product = Product(tenant_id=tenant_id, sku=sku, title=f"Produkt {sku}", **fields)
            db.add(product)
            db.flush()
            ids[sku] = str(product.id)
        db.commit()
    calls = []

    def orders(account, tenant, db):
        calls.append("orders")
        return {"created": new_orders}

    def fetch(account, db):
        calls.append("listings")
        return listings

    monkeypatch.setattr(ebay_router.ebay_order_service, "sync_orders", orders)
    monkeypatch.setattr(ebay_listing_service, "fetch_active_listings", fetch)
    monkeypatch.setattr(ebay_listing_service, "revise_listings", lambda *a: calls.append("WRITE TO EBAY"))
    return headers, ids, calls


def _stock(product_id):
    with SessionLocal() as db:
        return db.query(Product).filter(Product.id == product_id).one().stock_quantity


def test_preview_lists_differences_and_changes_nothing(register, client, monkeypatch):
    headers, ids, calls = _setup(
        register, client, monkeypatch,
        [_listing("111", 24), _listing("222", 5, sku="KAB"), _listing("999", 3)],
        {"111": {"stock_quantity": 0}, "KAB": {"stock_quantity": 5}, "123": {"stock_quantity": 5}})
    out = client.post("/ebay/pull-stock", headers=headers, json={}).json()
    assert [(i["sku"], i["shipsync_quantity"], i["ebay_quantity"]) for i in out["items"]] == [("111", 0, 24)]
    assert out["equal"] == 1 and out["without_listing"] == 1 and out["applied"] is False
    assert _stock(ids["111"]) == 0
    assert calls == ["orders", "listings"]  # orders first, nothing written to eBay


def test_only_confirmed_products_are_saved_and_logged(register, client, monkeypatch):
    headers, ids, calls = _setup(
        register, client, monkeypatch, [_listing("111", 24), _listing("222", 15)],
        {"111": {"stock_quantity": 0}, "222": {"stock_quantity": 25}})
    out = client.post("/ebay/pull-stock", headers=headers,
                      json={"dry_run": False, "confirmed": {ids["111"]: 24}}).json()
    assert out["updated"] == 1 and out["outdated"] == []
    assert _stock(ids["111"]) == 24 and _stock(ids["222"]) == 25
    with SessionLocal() as db:
        move = db.query(StockMovement).filter(StockMovement.product_id == ids["111"]).one()
        assert (move.quantity_change, move.reason, move.reference) == (24, "ebay_stock", "111")
    assert "WRITE TO EBAY" not in calls


def test_number_that_moved_since_the_preview_is_not_saved(register, client, monkeypatch):
    headers, ids, _ = _setup(register, client, monkeypatch, [_listing("111", 23)],
                             {"111": {"stock_quantity": 0}})
    out = client.post("/ebay/pull-stock", headers=headers,
                      json={"dry_run": False, "confirmed": {ids["111"]: 24}}).json()
    assert out["updated"] == 0 and out["outdated"] == [ids["111"]]
    assert _stock(ids["111"]) == 0


def test_two_listings_for_one_product_are_only_reported(register, client, monkeypatch):
    headers, ids, _ = _setup(register, client, monkeypatch,
                             [_listing("111", 4, sku="KAB"), _listing("222", 4, sku="KAB")],
                             {"KAB": {"stock_quantity": 1}})
    out = client.post("/ebay/pull-stock", headers=headers,
                      json={"dry_run": False, "confirmed": {ids["KAB"]: 4}}).json()
    assert out["items"] == [] and out["ambiguous"][0]["item_ids"] == ["111", "222"]
    assert out["updated"] == 0 and _stock(ids["KAB"]) == 1


def test_unlisted_and_deactivated_products_stay_untouched(register, client, monkeypatch):
    headers, _, _ = _setup(register, client, monkeypatch, [_listing("111", 9), _listing("222", 9)],
                           {"111": {"stock_quantity": 1, "unlisted": True},
                            "222": {"stock_quantity": 1, "active": False}})
    out = client.post("/ebay/pull-stock", headers=headers, json={}).json()
    assert out["items"] == [] and out["without_listing"] == 0


def test_failed_order_sync_stops_everything(register, client, monkeypatch):
    headers, ids, _ = _setup(register, client, monkeypatch, [_listing("111", 24)],
                             {"111": {"stock_quantity": 0}})

    def broken(*a):
        raise ValueError("Token abgelaufen")

    monkeypatch.setattr(ebay_router.ebay_order_service, "sync_orders", broken)
    r = client.post("/ebay/pull-stock", headers=headers,
                    json={"dry_run": False, "confirmed": {ids["111"]: 24}})
    assert r.status_code == 502 and "Token abgelaufen" in r.json()["detail"]
    assert _stock(ids["111"]) == 0


def test_saving_needs_a_selection(register, client, monkeypatch):
    headers, _, calls = _setup(register, client, monkeypatch, [], {})
    r = client.post("/ebay/pull-stock", headers=headers, json={"dry_run": False})
    assert r.status_code == 400 and calls == []
