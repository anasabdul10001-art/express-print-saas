"""Shopify: OAuth connect (HMAC + state), product import, order sync, shipping."""

import hashlib
import hmac
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from app.config import settings
from app.core.crypto import decrypt_token
from app.database import SessionLocal
from app.models.order import Order
from app.models.product import Product
from app.models.shopify_account import ShopifyAccount, ShopifyOAuthState
from app.models.stock_movement import StockMovement
from app.services import shopify_oauth_service, shopify_service

SECRET = "shpss_test_secret"


@pytest.fixture(autouse=True)
def shopify_config(monkeypatch):
    monkeypatch.setattr(settings, "shopify_client_id", "client-id")
    monkeypatch.setattr(settings, "shopify_client_secret", SECRET)


def _signed(params: dict) -> dict:
    message = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return {**params, "hmac": hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()}


def _tenant(register, client):
    headers, _ = register()
    return headers, client.get("/auth/me", headers=headers).json()["tenant_id"]


def _account(tenant_id, connected_at=None):
    with SessionLocal() as db:
        account = ShopifyAccount(
            id=uuid.uuid4(), tenant_id=tenant_id, shop_domain="demo.myshopify.com", status="CONNECTED",
            access_token_encrypted=shopify_oauth_service.encrypt_token("shpat_x"),
            connected_at=connected_at or datetime.now(timezone.utc) - timedelta(days=30),
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        return account


# ---------------------------------------------------------------- OAuth

@pytest.mark.parametrize("raw,expected", [
    ("my-store", "my-store.myshopify.com"),
    ("My-Store.myshopify.com", "my-store.myshopify.com"),
    ("https://my-store.myshopify.com/admin/products", "my-store.myshopify.com"),
])
def test_normalize_shop_accepts_store_names(raw, expected):
    assert shopify_oauth_service.normalize_shop(raw) == expected


@pytest.mark.parametrize("raw", ["", "evil.com", "shop.myshopify.com.evil.com", "a b.myshopify.com", "x.example.com"])
def test_normalize_shop_rejects_foreign_hosts(raw):
    with pytest.raises(ValueError):
        shopify_oauth_service.normalize_shop(raw)


def test_authorize_url_points_at_the_store_and_stores_state(register, client):
    headers, tenant_id = _tenant(register, client)
    r = client.get("/shopify/authorize-url", params={"shop": "demo"}, headers=headers)
    assert r.status_code == 200
    url = urlparse(r.json()["authorize_url"])
    assert url.netloc == "demo.myshopify.com" and url.path == "/admin/oauth/authorize"
    q = parse_qs(url.query)
    assert q["client_id"] == ["client-id"] and "read_orders" in q["scope"][0]
    with SessionLocal() as db:
        assert db.query(ShopifyOAuthState).filter_by(state=q["state"][0], tenant_id=tenant_id).one()


def test_authorize_url_rejects_bad_shop_and_missing_config(register, client, monkeypatch):
    headers, _ = _tenant(register, client)
    assert client.get("/shopify/authorize-url", params={"shop": "evil.com"}, headers=headers).status_code == 400
    monkeypatch.setattr(settings, "shopify_client_secret", "")
    assert client.get("/shopify/authorize-url", params={"shop": "demo"}, headers=headers).status_code == 503


def _start(client, headers, shop="demo"):
    url = client.get("/shopify/authorize-url", params={"shop": shop}, headers=headers).json()["authorize_url"]
    return parse_qs(urlparse(url).query)["state"][0]


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code, self.text = payload, status, str(payload)

    def json(self):
        return self._payload


def test_callback_stores_encrypted_token_and_connects(register, client, monkeypatch):
    headers, tenant_id = _tenant(register, client)
    state = _start(client, headers)
    monkeypatch.setattr(shopify_oauth_service.httpx, "post",
                        lambda url, **k: _Resp({"access_token": "shpat_secret", "scope": "read_orders"}))
    query = _signed({"code": "abc", "shop": "demo.myshopify.com", "state": state, "timestamp": "1"})
    r = client.get("/shopify/callback?" + urlencode(query), follow_redirects=False)
    assert r.status_code in (302, 307) and "shopify_connected=demo.myshopify.com" in r.headers["location"]
    with SessionLocal() as db:
        account = db.query(ShopifyAccount).filter_by(tenant_id=tenant_id).one()
        assert account.status == "CONNECTED" and account.scopes == "read_orders"
        assert "shpat_secret" not in account.access_token_encrypted
        assert decrypt_token(account.access_token_encrypted) == "shpat_secret"
        assert db.query(ShopifyOAuthState).filter_by(state=state).first() is None  # one-time
    assert [a["shop_domain"] for a in client.get("/shopify/accounts", headers=headers).json()] == ["demo.myshopify.com"]


def test_callback_rejects_bad_signature_replay_and_other_shop(register, client, monkeypatch):
    headers, tenant_id = _tenant(register, client)
    monkeypatch.setattr(shopify_oauth_service.httpx, "post", lambda url, **k: _Resp({"access_token": "t"}))

    state = _start(client, headers)
    bad = dict(_signed({"code": "abc", "shop": "demo.myshopify.com", "state": state}), hmac="0" * 64)
    r = client.get("/shopify/callback?" + urlencode(bad), follow_redirects=False)
    assert "shopify_error=invalid_hmac" in r.headers["location"]

    good = _signed({"code": "abc", "shop": "demo.myshopify.com", "state": state})
    assert "shopify_connected" in client.get("/shopify/callback?" + urlencode(good), follow_redirects=False).headers["location"]
    replay = client.get("/shopify/callback?" + urlencode(good), follow_redirects=False)
    assert "shopify_error=invalid_or_unknown_state" in replay.headers["location"]

    state2 = _start(client, headers)
    other = _signed({"code": "abc", "shop": "other.myshopify.com", "state": state2})
    r = client.get("/shopify/callback?" + urlencode(other), follow_redirects=False)
    assert "shopify_error=shop_mismatch" in r.headers["location"]


def test_callback_rejects_expired_state(register, client, monkeypatch):
    headers, _ = _tenant(register, client)
    state = _start(client, headers)
    with SessionLocal() as db:
        row = db.query(ShopifyOAuthState).filter_by(state=state).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    q = _signed({"code": "abc", "shop": "demo.myshopify.com", "state": state})
    r = client.get("/shopify/callback?" + urlencode(q), follow_redirects=False)
    assert "shopify_error=authorization_request_expired" in r.headers["location"]


def test_disconnect_wipes_token(register, client):
    headers, tenant_id = _tenant(register, client)
    account = _account(tenant_id)
    assert client.post("/shopify/disconnect", params={"account_id": str(account.id)}, headers=headers).status_code == 200
    with SessionLocal() as db:
        row = db.get(ShopifyAccount, account.id)
        assert row.status == "DISCONNECTED" and row.access_token_encrypted is None
    assert client.get("/shopify/accounts", headers=headers).json() == []


# ------------------------------------------------------------- products

def _product_page(nodes, has_next=False):
    return {"shop": {"name": "Demo Shop"}, "products": {"pageInfo": {"hasNextPage": has_next, "endCursor": "c"}, "nodes": nodes}}


def test_import_products_creates_variants_and_skips_known(register, client, monkeypatch):
    _, tenant_id = _tenant(register, client)
    account = _account(tenant_id)
    nodes = [
        {"title": "Kabel", "status": "ACTIVE", "featuredImage": {"url": "http://img/k.jpg"}, "variants": {"nodes": [
            {"id": "gid://shopify/ProductVariant/11", "sku": "K-1", "title": "Default Title", "price": "4.50", "inventoryQuantity": 7, "image": None},
            {"id": "gid://shopify/ProductVariant/12", "sku": "", "title": "Schwarz", "price": "4.90", "inventoryQuantity": -2, "image": {"url": "http://img/s.jpg"}},
        ]}},
        {"title": "Archiviert", "status": "ARCHIVED", "variants": {"nodes": [
            {"id": "gid://shopify/ProductVariant/13", "sku": "OLD", "title": "Default Title", "price": "1", "inventoryQuantity": 1}]}},
    ]
    monkeypatch.setattr(shopify_service, "graphql", lambda a, q, v=None: _product_page(nodes))
    with SessionLocal() as db:
        db.add(Product(tenant_id=tenant_id, sku="K-1", title="schon da", stock_quantity=1))
        db.commit()
        result = shopify_service.import_products(db.get(ShopifyAccount, account.id), tenant_id, db)
        assert result == {"listings_found": 2, "products_created": 1, "already_known": 1}
        assert db.get(ShopifyAccount, account.id).shop_name == "Demo Shop"
        made = db.query(Product).filter_by(tenant_id=tenant_id, sku="12").one()  # no SKU -> variant id
        assert made.title == "Kabel - Schwarz" and float(made.selling_price) == 4.90
        assert made.stock_quantity == 0 and made.image_url == "http://img/s.jpg"
        assert db.query(Product).filter_by(tenant_id=tenant_id, sku="K-1").one().title == "schon da"  # untouched
        assert db.query(Product).filter_by(tenant_id=tenant_id, sku="OLD").first() is None


def test_import_products_endpoint(register, client, monkeypatch):
    headers, tenant_id = _tenant(register, client)
    _account(tenant_id)
    monkeypatch.setattr(shopify_service, "graphql", lambda a, q, v=None: _product_page([
        {"title": "A", "status": "ACTIVE", "variants": {"nodes": [{"id": "gid://shopify/ProductVariant/1", "sku": "A1", "title": "Default Title", "price": "1.00", "inventoryQuantity": 3}]}}]))
    r = client.post("/shopify/import-products", headers=headers)
    assert r.status_code == 200 and r.json()["products_created"] == 1


# --------------------------------------------------------------- orders

def _line(sku, qty=1, price="10.00", variant=None, title="Artikel"):
    return {"title": title, "quantity": qty, "sku": sku,
            "variant": {"id": f"gid://shopify/ProductVariant/{variant}"} if variant else None,
            "originalUnitPriceSet": {"shopMoney": {"amount": price}},
            "discountedUnitPriceSet": {"shopMoney": {"amount": price}}}


def _order(number, lines, created=None, refunded="0.00", fees=None, cancelled=None, fulfilled="UNFULFILLED", address=True):
    return {
        "id": f"gid://shopify/Order/{number}", "name": f"#{number}",
        "createdAt": (created or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cancelledAt": cancelled, "displayFulfillmentStatus": fulfilled,
        "totalRefundedSet": {"shopMoney": {"amount": refunded}},
        "transactions": [{"fees": [{"amount": {"amount": f}} for f in (fees or [])]}],
        "lineItems": {"nodes": lines},
        "customer": {"displayName": "Erika Muster"},
        "shippingAddress": {"name": "Erika Muster", "address1": "Hauptstr. 1", "address2": None, "city": "Döbeln",
                            "province": None, "zip": "04720", "countryCodeV2": "DE", "phone": None} if address else None,
    }


def _sync(tenant_id, monkeypatch, orders, connected_at=None):
    account = _account(tenant_id, connected_at)
    monkeypatch.setattr(shopify_service, "graphql", lambda a, q, v=None: {"orders": {"nodes": orders}})
    with SessionLocal() as db:
        return account, shopify_service.sync_orders(db.get(ShopifyAccount, account.id), tenant_id, db)


def test_order_sync_imports_order_with_address_refund_and_fees(register, client, monkeypatch):
    _, tenant_id = _tenant(register, client)
    with SessionLocal() as db:
        db.add(Product(tenant_id=tenant_id, sku="K-1", title="Kabel", stock_quantity=5, purchase_price=2))
        db.commit()
    orders = [_order(1001, [_line("K-1", qty=2, price="9.99")], refunded="9.99", fees=["0.45", "0.30"])]
    account, result = _sync(tenant_id, monkeypatch, orders)
    assert result["created"] == 1 and result["skipped"] == 0
    with SessionLocal() as db:
        order = db.query(Order).filter_by(tenant_id=tenant_id).one()
        assert order.external_order_ref == "shopify:1001" and order.status == "new"
        assert order.shopify_account_id == account.id and order.ebay_account_id is None
        assert (order.recipient_name, order.recipient_zip, order.recipient_country_code) == ("Erika Muster", "04720", "DE")
        assert float(order.refund_amount) == 9.99 and float(order.ebay_fees) == 0.75
        assert order.buyer_username == "Erika Muster"
        item = order.items[0]
        assert item.quantity == 2 and float(item.unit_price) == 9.99 and float(item.purchase_price) == 2.0
        assert db.query(Product).filter_by(tenant_id=tenant_id, sku="K-1").one().stock_quantity == 3
        assert db.query(StockMovement).filter_by(reference="shopify:1001").one().quantity_change == -2


def test_order_sync_is_idempotent_and_updates_refund_and_fees(register, client, monkeypatch):
    _, tenant_id = _tenant(register, client)
    account, _ = _sync(tenant_id, monkeypatch, [_order(1, [_line("S", price="5.00")])])
    monkeypatch.setattr(shopify_service, "graphql", lambda a, q, v=None: {"orders": {"nodes": [
        _order(1, [_line("S", price="5.00")], refunded="5.00", fees=["0.40"])]}})
    with SessionLocal() as db:
        again = shopify_service.sync_orders(db.get(ShopifyAccount, account.id), tenant_id, db)
        assert again["created"] == 0
        orders = db.query(Order).filter_by(tenant_id=tenant_id).all()
        assert len(orders) == 1 and float(orders[0].refund_amount) == 5.0 and float(orders[0].ebay_fees) == 0.40


def test_order_sync_creates_unknown_product_and_uses_variant_id_without_sku(register, client, monkeypatch):
    _, tenant_id = _tenant(register, client)
    orders = [_order(7, [_line(None, variant=555, price="3.00", title="Ohne SKU")])]
    _, result = _sync(tenant_id, monkeypatch, orders)
    assert result["created"] == 1 and result["products_created"] == 1
    with SessionLocal() as db:
        product = db.query(Product).filter_by(tenant_id=tenant_id, sku="555").one()
        assert product.title == "Ohne SKU" and float(product.selling_price) == 3.0


def test_order_sync_skips_line_without_sku_or_variant(register, client, monkeypatch):
    _, tenant_id = _tenant(register, client)
    _, result = _sync(tenant_id, monkeypatch, [_order(8, [_line(None, title="Geloescht")])])
    assert result["created"] == 0 and result["skipped"] == 1 and "#8" in result["skipped_reasons"][0]


def test_old_orders_do_not_touch_stock_and_cancelled_or_fulfilled_get_their_status(register, client, monkeypatch):
    _, tenant_id = _tenant(register, client)
    with SessionLocal() as db:
        db.add(Product(tenant_id=tenant_id, sku="P", title="P", stock_quantity=10))
        db.commit()
    now = datetime.now(timezone.utc)
    orders = [
        _order(1, [_line("P")], created=now - timedelta(days=90)),                       # before connecting
        _order(2, [_line("P")], created=now, cancelled="2026-11-20T10:00:00Z"),          # cancelled
        _order(3, [_line("P")], created=now, fulfilled="FULFILLED"),                     # already shipped, new sale
    ]
    _, result = _sync(tenant_id, monkeypatch, orders)
    assert result["created"] == 3
    with SessionLocal() as db:
        statuses = {o.external_order_ref: o.status for o in db.query(Order).filter_by(tenant_id=tenant_id)}
        assert statuses == {"shopify:1": "new", "shopify:2": "canceled", "shopify:3": "shipped"}
        assert db.query(Product).filter_by(tenant_id=tenant_id, sku="P").one().stock_quantity == 9  # only order 3


def test_order_sync_falls_back_without_customer_data_when_not_approved(register, client, monkeypatch):
    _, tenant_id = _tenant(register, client)
    account = _account(tenant_id)
    calls = []

    def fake(a, query, variables=None):
        calls.append("shippingAddress" in query)
        if "shippingAddress" in query:
            raise shopify_service.ShopifyAccessDenied("This app is not approved to access the Customer object")
        return {"orders": {"nodes": [_order(5, [_line("Q")], address=False)]}}

    monkeypatch.setattr(shopify_service, "graphql", fake)
    with SessionLocal() as db:
        result = shopify_service.sync_orders(db.get(ShopifyAccount, account.id), tenant_id, db)
        assert calls == [True, False] and result["created"] == 1 and "freigegeben" in result["notes"][0]
        assert db.query(Order).filter_by(tenant_id=tenant_id).one().recipient_street1 is None


def test_sync_orders_endpoint_and_orders_list_shows_shopify_source(register, client, monkeypatch):
    headers, tenant_id = _tenant(register, client)
    _account(tenant_id)
    monkeypatch.setattr(shopify_service, "graphql", lambda a, q, v=None: {"orders": {"nodes": [_order(9, [_line("E")])]}})
    r = client.post("/shopify/sync-orders", headers=headers)
    assert r.status_code == 200 and r.json()["created"] == 1
    listed = client.get("/orders", headers=headers).json()
    assert listed[0]["source"] == "shopify" and listed[0]["shopify_account_name"] == "demo.myshopify.com"


def test_sync_orders_without_store_is_400(register, client):
    headers, _ = _tenant(register, client)
    assert client.post("/shopify/sync-orders", headers=headers).status_code == 400


def test_graphql_marks_access_errors_and_unauthorized(register, client, monkeypatch):
    _, tenant_id = _tenant(register, client)
    account = _account(tenant_id)
    monkeypatch.setattr(shopify_service.httpx, "post", lambda *a, **k: _Resp({"errors": [{"message": "ACCESS_DENIED Customer"}]}))
    with pytest.raises(shopify_service.ShopifyAccessDenied):
        shopify_service.graphql(account, "query { shop { name } }")
    monkeypatch.setattr(shopify_service.httpx, "post", lambda *a, **k: _Resp({}, status=401))
    with pytest.raises(ValueError, match="reconnect"):
        shopify_service.graphql(account, "query { shop { name } }")


# ------------------------------------------------------------- shipping

def _shopify_order(tenant_id, account_id, ref="shopify:42"):
    with SessionLocal() as db:
        order = Order(tenant_id=tenant_id, external_order_ref=ref, status="new", shopify_account_id=account_id)
        db.add(order)
        db.commit()
        return order.id


def test_ship_creates_fulfillment_with_tracking(register, client, monkeypatch):
    headers, tenant_id = _tenant(register, client)
    account = _account(tenant_id)
    order_id = _shopify_order(tenant_id, account.id)
    sent = []

    def fake(a, query, variables=None):
        sent.append(variables)
        if "fulfillmentOrders" in query:
            return {"order": {"fulfillmentOrders": {"nodes": [{"id": "gid://shopify/FulfillmentOrder/1", "status": "OPEN"},
                                                              {"id": "gid://shopify/FulfillmentOrder/2", "status": "CLOSED"}]}}}
        return {"fulfillmentCreate": {"fulfillment": {"id": "f", "status": "SUCCESS"}, "userErrors": []}}

    monkeypatch.setattr(shopify_service, "graphql", fake)
    r = client.post(f"/shopify/orders/{order_id}/ship", json={"tracking_number": "0034 1234-56", "carrier": "DHL"}, headers=headers)
    assert r.status_code == 200 and r.json() == {"status": "shipped", "tracking_number": "0034123456"}
    assert sent[0] == {"id": "gid://shopify/Order/42"}
    fulfilment = sent[1]["f"]
    assert fulfilment["lineItemsByFulfillmentOrder"] == [{"fulfillmentOrderId": "gid://shopify/FulfillmentOrder/1"}]
    assert fulfilment["trackingInfo"] == {"number": "0034123456", "company": "DHL"} and fulfilment["notifyCustomer"] is True


def test_ship_reports_shopify_errors_and_rejects_foreign_orders(register, client, monkeypatch):
    headers, tenant_id = _tenant(register, client)
    account = _account(tenant_id)
    order_id = _shopify_order(tenant_id, account.id)

    def fake(a, query, variables=None):
        if "fulfillmentOrders" in query:
            return {"order": {"fulfillmentOrders": {"nodes": [{"id": "gid://shopify/FulfillmentOrder/1", "status": "OPEN"}]}}}
        return {"fulfillmentCreate": {"fulfillment": None, "userErrors": [{"field": ["x"], "message": "Tracking invalid"}]}}

    monkeypatch.setattr(shopify_service, "graphql", fake)
    r = client.post(f"/shopify/orders/{order_id}/ship", json={"tracking_number": "123", "carrier": "DHL"}, headers=headers)
    assert r.status_code == 502 and "Tracking invalid" in r.json()["detail"]
    with SessionLocal() as db:
        assert db.get(Order, order_id).status == "new"

    ebay_order = _shopify_order(tenant_id, None, ref="21-15411-18885")
    r = client.post(f"/shopify/orders/{ebay_order}/ship", json={"tracking_number": "123", "carrier": "DHL"}, headers=headers)
    assert r.status_code == 400
    assert client.post(f"/shopify/orders/{order_id}/ship", json={"tracking_number": "1", "carrier": "NOPE"}, headers=headers).status_code == 400
