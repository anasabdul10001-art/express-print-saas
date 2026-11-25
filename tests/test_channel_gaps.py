"""Pushing to Shopify and pointing out Amazon refunds. Both platforms are faked."""
from app.database import SessionLocal
from app.models.amazon_account import AmazonAccount
from app.models.order import Order
from app.models.shopify_account import ShopifyAccount
from app.services import amazon_service, shopify_service


class Shop:
    def __init__(self, locations=1, stale=False):
        self.mutations, self.locations, self.stale = [], locations, stale

    def graphql(self, account, query, variables=None):
        if "productVariants(first: 100" in query:
            return {"productVariants": {"pageInfo": {"hasNextPage": False}, "nodes": [
                {"id": "gid://shopify/ProductVariant/1", "sku": "A", "price": "5.00", "inventoryQuantity": 2,
                 "product": {"id": "gid://shopify/Product/9"}, "inventoryItem": {"id": "gid://shopify/InventoryItem/7"}},
                {"id": "gid://shopify/ProductVariant/2", "sku": "UNKNOWN", "price": "1.00", "inventoryQuantity": 0,
                 "product": {"id": "gid://shopify/Product/9"}, "inventoryItem": {"id": "x"}}]}}
        if "locations(first" in query:
            return {"locations": {"nodes": [{"id": f"loc{i}"} for i in range(self.locations)]}}
        self.mutations.append((query, variables))
        if "inventorySetQuantities" in query:
            errors = [{"message": "stale"}] if self.stale else []
            return {"inventorySetQuantities": {"userErrors": errors}}
        return {"productVariantsBulkUpdate": {"userErrors": []}}


def _setup(client, register, monkeypatch, shop):
    headers, _ = register()
    tenant_id = client.get("/auth/me", headers=headers).json()["tenant_id"]
    with SessionLocal() as db:
        db.add(ShopifyAccount(tenant_id=tenant_id, shop_domain="t.myshopify.com", status="CONNECTED"))
        db.commit()
    client.post("/products", headers=headers, json={"sku": "A", "title": "A", "selling_price": 6.5, "stock_quantity": 8})
    monkeypatch.setattr(shopify_service, "graphql", shop.graphql)
    return headers


def _push(client, headers, **body):
    r = client.post("/shopify/push-listings", headers=headers, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_comparing_writes_nothing(client, register, monkeypatch):
    shop = Shop()
    headers = _setup(client, register, monkeypatch, shop)
    data = _push(client, headers, include_quantity=True)
    assert data["applied"] is False and data["to_change"] == 1 and shop.mutations == []
    item = data["items"][0]
    assert (item["shop_price"], item["new_price"], item["shop_quantity"], item["new_quantity"]) == ("5.00", "6.50", 2, 8)
    assert not any(key.endswith("_id") for key in item)


def test_apply_sends_price_and_guarded_quantity(client, register, monkeypatch):
    shop = Shop()
    headers = _setup(client, register, monkeypatch, shop)
    assert _push(client, headers, include_quantity=True, dry_run=False)["updated"] == 1
    price, stock = shop.mutations[0][1], shop.mutations[1][1]
    assert price["variants"] == [{"id": "gid://shopify/ProductVariant/1", "price": "6.50"}]
    assert stock["input"]["quantities"][0] == {"inventoryItemId": "gid://shopify/InventoryItem/7",
                                               "locationId": "loc0", "quantity": 8, "changeFromQuantity": 2}
    assert "@idempotent" in shop.mutations[1][0] and stock["key"]


def test_several_locations_or_stale_stock_are_reported(client, register, monkeypatch):
    shop = Shop(locations=2)
    headers = _setup(client, register, monkeypatch, shop)
    data = _push(client, headers, include_price=False, include_quantity=True, dry_run=False)
    assert data["failed"] == 1 and "Lagerorte" in data["items"][0]["errors"][0] and shop.mutations == []
    shop.locations, shop.stale = 1, True
    data = _push(client, headers, include_price=False, include_quantity=True, dry_run=False)
    assert data["items"][0]["errors"] == ["Menge: stale"]


def test_amazon_refund_is_noted_but_no_amount_is_written(client, register, monkeypatch):
    headers, _ = register()
    tenant_id = client.get("/auth/me", headers=headers).json()["tenant_id"]
    with SessionLocal() as db:
        account = AmazonAccount(tenant_id=tenant_id, seller_id="S", status="CONNECTED")
        db.add(account)
        db.add(Order(tenant_id=tenant_id, external_order_ref=amazon_service.REF_PREFIX + "111", status="shipped"))
        db.commit()
        transactions = [
            {"description": "Refund Order", "relatedIdentifiers": [{"relatedIdentifierName": "ORDER_ID", "relatedIdentifierValue": "111"}]},
            {"description": "Order Payment", "relatedIdentifiers": [{"relatedIdentifierName": "ORDER_ID", "relatedIdentifierValue": "222"}]},
        ]
        monkeypatch.setattr(amazon_service, "call", lambda *a, **k: {"payload": {"transactions": transactions}})
        notes = []
        amazon_service.note_refunds(account, account.tenant_id, db, notes)
        assert len(notes) == 1 and "111" in notes[0]
        assert db.query(Order).filter(Order.tenant_id == tenant_id).one().refund_amount is None
