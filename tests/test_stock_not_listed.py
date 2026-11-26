"""Goods in stock that are not offered yet, and joining them with their listing."""

import json
from decimal import Decimal

from app.database import SessionLocal
from app.models.product import Product
from app.services import ebay_listing_service, stock_listing_service


def _new(client, headers, sku, **extra):
    r = client.post("/products", headers=headers, json={"sku": sku, "title": f"Ware {sku}", **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _all(client, headers):
    return {p["sku"]: p for p in client.get("/products", headers=headers).json()}


def _tenant(client, headers):
    return client.get("/auth/me", headers=headers).json()["tenant_id"]


def test_stock_goods_are_kept_apart_and_are_no_expense(register, client):
    headers, _ = register()
    made = _new(client, headers, "L-1", unlisted=True, stock_quantity=10, purchase_price=2, acquired_on="2026-11-01")
    assert made["unlisted"] is True and made["acquired_on"] == "2026-11-01"
    assert _new(client, headers, "N-1")["unlisted"] is False
    report = client.get("/accounting/euer?start=2026-01-01&end=2026-12-31", headers=headers).json()
    assert report["total"]["costs"] == "0.00"


def test_linking_keeps_one_product_with_history_and_the_counted_quantity(register, client):
    headers, _ = register()
    stock = _new(client, headers, "L-1", unlisted=True, stock_quantity=0, shelf_location="A1")
    listing = _new(client, headers, "E-1", stock_quantity=4, selling_price=9)
    data = {"supplier": "S", "invoice_number": "1", "invoice_date": "2026-11-01",
            "lines": [{"product_id": stock["id"], "quantity": 10, "line_total": "20.00"}],
            "supplier_items": [{"name": "Cable X", "product_id": stock["id"]}]}
    assert client.post("/purchases", headers=headers, data={"data": json.dumps(data)},
                       files={"file": ("r.pdf", b"%PDF-1.4 x", "application/pdf")}).status_code == 201
    url = f"/products/{stock['id']}/link"
    r = client.post(url, headers=headers, json={"listing_product_id": listing["id"], "stock_quantity": 10})
    assert r.status_code == 200, r.text
    joined = r.json()
    assert (joined["sku"], joined["stock_quantity"], joined["shelf_location"]) == ("E-1", 10, "A1")  # not 14
    assert Decimal(joined["purchase_price"]) == Decimal("2.00") and joined["unlisted"] is False
    assert list(_all(client, headers)) == ["E-1"]
    found = client.post("/purchases/match", headers=headers,
                        json={"supplier": "S", "items": [{"name": "Cable X"}]}).json()[0]
    assert found["match"]["product_id"] == listing["id"]
    # the joined row cannot be linked a second time
    again = client.post(url, headers=headers, json={"listing_product_id": listing["id"], "stock_quantity": 1})
    assert again.status_code == 404


def test_different_costs_need_a_choice(register, client):
    headers, _ = register()
    stock = _new(client, headers, "L-1", unlisted=True, stock_quantity=5, purchase_price=2)
    listing = _new(client, headers, "E-1", stock_quantity=5, purchase_price=3)
    url = f"/products/{stock['id']}/link"
    body = {"listing_product_id": listing["id"], "stock_quantity": 5}
    assert client.post(url, headers=headers, json=body).status_code == 422
    assert _all(client, headers)["L-1"]["unlisted"] is True  # nothing happened
    joined = client.post(url, headers=headers, json={**body, "cost_from": "stock"}).json()
    assert Decimal(joined["purchase_price"]) == Decimal("2.00")


def test_wrong_pairs_and_other_accounts_are_refused(register, client):
    headers, _ = register()
    other, _ = register()
    stock = _new(client, headers, "L-1", unlisted=True, stock_quantity=1)
    second = _new(client, headers, "L-2", unlisted=True, stock_quantity=1)
    listing = _new(client, headers, "E-1")

    def link(source, target, who=headers):
        return client.post(f"/products/{source}/link", headers=who,
                           json={"listing_product_id": target, "stock_quantity": 1}).status_code

    assert link(stock["id"], stock["id"]) == 400
    assert link(stock["id"], second["id"]) == 400
    assert link(listing["id"], stock["id"]) == 400
    assert link(stock["id"], listing["id"], other) == 404


def test_same_sku_in_the_shop_ends_the_stock_state_without_touching_the_quantity(register, client, monkeypatch):
    headers, _ = register()
    _new(client, headers, "SAME", unlisted=True, stock_quantity=30)
    _new(client, headers, "OTHER", unlisted=True, stock_quantity=2)
    monkeypatch.setattr(ebay_listing_service, "fetch_active_listings", lambda account, db: [
        {"item_id": "111", "sku": "SAME", "title": "Angebot", "price": Decimal("5"), "available": 4, "image_url": None}])
    with SessionLocal() as db:
        result = ebay_listing_service.import_listings(None, _tenant(client, headers), db)
    assert result["stock_linked"] == 1 and result["products_created"] == 0
    rows = _all(client, headers)
    assert rows["SAME"]["unlisted"] is False and rows["SAME"]["stock_quantity"] == 30
    assert rows["OTHER"]["unlisted"] is True


def test_a_stock_product_stops_being_unlisted_only_once(register, client):
    headers, _ = register()
    _new(client, headers, "L-1", unlisted=True, stock_quantity=0)
    with SessionLocal() as db:
        product = db.query(Product).filter(Product.tenant_id == _tenant(client, headers)).one()
        assert stock_listing_service.now_listed(db, product) is True
        assert stock_listing_service.now_listed(db, product) is False
