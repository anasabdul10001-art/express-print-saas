"""Pushing stock quantity (and/or price) to eBay listings."""
from decimal import Decimal
from unittest.mock import patch

from app.database import SessionLocal
from app.models.ebay_account import EbayAccount
from app.services import ebay_listing_service


def test_revise_xml_sends_only_given_fields():
    xml = ebay_listing_service._revise_xml([("1", Decimal("2.50"), None), ("2", None, 7)])
    assert "<ItemID>1</ItemID><StartPrice>2.50</StartPrice></InventoryStatus>" in xml
    assert "<ItemID>2</ItemID><Quantity>7</Quantity></InventoryStatus>" in xml
    assert "<Quantity>" not in xml.split("<ItemID>2</ItemID>")[0]


def test_push_quantity_only_changes_quantity(client, register):
    headers, _ = register()
    client.post("/products", headers=headers, json={"sku": "Q1", "title": "Q1", "selling_price": 3, "stock_quantity": 9})
    pid = client.get("/products", headers=headers).json()[0]["id"]
    listings = [{"item_id": "111", "sku": "Q1", "title": "x", "price": Decimal("5.00"), "available": 4, "image_url": None}]
    sent = []

    def fake_revise(account, db, items):
        sent.extend(items)
        return {i[0]: None for i in items}

    with patch("app.routers.ebay._connected_accounts") as acc, \
         patch("app.routers.ebay.ebay_listing_service.fetch_active_listings", return_value=listings), \
         patch("app.routers.ebay.ebay_listing_service.revise_listings", side_effect=fake_revise):
        acc.return_value.all.return_value = [EbayAccount(ebay_user_id="me", environment="PRODUCTION")]
        r = client.post("/ebay/push-prices", headers=headers,
                        json={"product_ids": [pid], "dry_run": False, "include_price": False, "include_quantity": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["updated"] == 1
    assert sent == [("111", None, 9)]


def test_list_products_has_sold_quantity(client, register):
    headers, _ = register()
    a = client.post("/products", headers=headers, json={"sku": "S1", "title": "S1", "selling_price": 3, "purchase_price": 1, "stock_quantity": 10}).json()["id"]
    b = client.post("/products", headers=headers, json={"sku": "S2", "title": "S2", "selling_price": 3, "purchase_price": 1, "stock_quantity": 10}).json()["id"]
    for pid, qty in ((a, 2), (a, 3), (b, 1)):
        assert client.post("/orders", headers=headers, json={"items": [{"product_id": pid, "quantity": qty}]}).status_code in (200, 201)
    sold = {p["sku"]: p["sold_quantity"] for p in client.get("/products", headers=headers).json()}
    assert sold == {"S1": 5, "S2": 1}
