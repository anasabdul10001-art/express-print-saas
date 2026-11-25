"""Amazon SP-API: authorization, listings import, order sync, fees, shipment confirmation."""

import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from app.config import settings
from app.core.crypto import decrypt_token, encrypt_token
from app.database import SessionLocal
from app.models.amazon_account import AmazonAccount, AmazonOAuthState
from app.models.order import Order
from app.models.product import Product
from app.models.stock_movement import StockMovement
from app.services import amazon_oauth_service, amazon_service

SELLER = "A1B2C3D4E5F6G7"


@pytest.fixture(autouse=True)
def amazon_config(monkeypatch):
    monkeypatch.setattr(settings, "amazon_lwa_client_id", "amzn1.application-oa2-client.test")
    monkeypatch.setattr(settings, "amazon_lwa_client_secret", "lwa-secret")
    monkeypatch.setattr(settings, "amazon_application_id", "amzn1.sp.solution.test")
    monkeypatch.setattr(settings, "amazon_draft_app", True)
    amazon_oauth_service._token_cache.clear()


class _Resp:
    def __init__(self, payload=None, status=200, text=None):
        self._payload, self.status_code = payload if payload is not None else {}, status
        self.text = text or str(payload)
        self.content = b"x" if payload is not None else b""

    def json(self):
        return self._payload


def _tenant(register, client):
    headers, _ = register()
    return headers, client.get("/auth/me", headers=headers).json()["tenant_id"]


def _account(tenant_id, connected_at=None, last_sync_at=None):
    with SessionLocal() as db:
        account = AmazonAccount(
            id=uuid.uuid4(), tenant_id=tenant_id, seller_id=SELLER, marketplace_id="A1PA6795UKMFR9", status="CONNECTED",
            refresh_token_encrypted=encrypt_token("Atzr|refresh"),
            connected_at=connected_at or datetime.now(timezone.utc) - timedelta(days=30), last_sync_at=last_sync_at,
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        return account


# ---------------------------------------------------------------- auth

def test_authorize_url_points_at_seller_central_and_stores_state(register, client):
    headers, tenant_id = _tenant(register, client)
    r = client.get("/amazon/authorize-url", headers=headers)
    assert r.status_code == 200
    url = urlparse(r.json()["authorize_url"])
    assert url.netloc == "sellercentral-europe.amazon.com" and url.path == "/apps/authorize/consent"
    q = parse_qs(url.query)
    assert q["application_id"] == ["amzn1.sp.solution.test"] and q["version"] == ["beta"]
    with SessionLocal() as db:
        assert db.query(AmazonOAuthState).filter_by(state=q["state"][0], tenant_id=tenant_id).one()


def test_authorize_url_without_beta_flag_and_without_config(register, client, monkeypatch):
    headers, _ = _tenant(register, client)
    monkeypatch.setattr(settings, "amazon_draft_app", False)
    q = parse_qs(urlparse(client.get("/amazon/authorize-url", headers=headers).json()["authorize_url"]).query)
    assert "version" not in q
    monkeypatch.setattr(settings, "amazon_application_id", "")
    assert client.get("/amazon/authorize-url", headers=headers).status_code == 503


def _start(client, headers):
    url = client.get("/amazon/authorize-url", headers=headers).json()["authorize_url"]
    return parse_qs(urlparse(url).query)["state"][0]


def test_callback_exchanges_code_and_stores_encrypted_refresh_token(register, client, monkeypatch):
    headers, tenant_id = _tenant(register, client)
    state = _start(client, headers)
    sent = {}

    def fake_post(url, data=None, **k):
        sent.update(url=url, data=data)
        return _Resp({"refresh_token": "Atzr|secret", "access_token": "a", "expires_in": 3600})

    monkeypatch.setattr(amazon_oauth_service.httpx, "post", fake_post)
    q = urlencode({"spapi_oauth_code": "code123", "selling_partner_id": SELLER, "state": state})
    r = client.get("/amazon/callback?" + q, follow_redirects=False)
    assert r.status_code in (302, 307) and f"amazon_connected={SELLER}" in r.headers["location"]
    assert sent["url"] == "https://api.amazon.com/auth/o2/token"
    assert sent["data"]["grant_type"] == "authorization_code" and sent["data"]["code"] == "code123"
    assert sent["data"]["client_secret"] == "lwa-secret"
    with SessionLocal() as db:
        account = db.query(AmazonAccount).filter_by(tenant_id=tenant_id).one()
        assert account.seller_id == SELLER and account.status == "CONNECTED" and account.marketplace_id == "A1PA6795UKMFR9"
        assert "Atzr|secret" not in account.refresh_token_encrypted
        assert decrypt_token(account.refresh_token_encrypted) == "Atzr|secret"
        assert db.query(AmazonOAuthState).filter_by(state=state).first() is None  # one-time
    assert [a["seller_id"] for a in client.get("/amazon/accounts", headers=headers).json()] == [SELLER]


def test_callback_rejects_unknown_state_replay_expiry_and_missing_params(register, client, monkeypatch):
    headers, _ = _tenant(register, client)
    monkeypatch.setattr(amazon_oauth_service.httpx, "post", lambda *a, **k: _Resp({"refresh_token": "r"}))

    def go(**p):
        return client.get("/amazon/callback?" + urlencode(p), follow_redirects=False).headers["location"]

    assert "amazon_error=invalid_or_unknown_state" in go(spapi_oauth_code="c", selling_partner_id=SELLER, state="nope")
    assert "amazon_error=missing_params" in go(state="x")

    state = _start(client, headers)
    assert "amazon_connected" in go(spapi_oauth_code="c", selling_partner_id=SELLER, state=state)
    assert "amazon_error=invalid_or_unknown_state" in go(spapi_oauth_code="c", selling_partner_id=SELLER, state=state)

    state = _start(client, headers)
    with SessionLocal() as db:
        row = db.query(AmazonOAuthState).filter_by(state=state).one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    assert "amazon_error=authorization_request_expired" in go(spapi_oauth_code="c", selling_partner_id=SELLER, state=state)


def test_callback_reports_amazon_rejecting_the_code(register, client, monkeypatch):
    headers, tenant_id = _tenant(register, client)
    state = _start(client, headers)
    monkeypatch.setattr(amazon_oauth_service.httpx, "post", lambda *a, **k: _Resp({"error": "invalid_grant"}, status=400))
    q = urlencode({"spapi_oauth_code": "bad", "selling_partner_id": SELLER, "state": state})
    assert "amazon_error=amazon_lwa_failed" in client.get("/amazon/callback?" + q, follow_redirects=False).headers["location"]
    with SessionLocal() as db:
        assert db.query(AmazonAccount).filter_by(tenant_id=tenant_id).first() is None


def test_connect_token_validates_the_token_before_storing_it(register, client, monkeypatch):
    headers, tenant_id = _tenant(register, client)
    monkeypatch.setattr(amazon_oauth_service.httpx, "post", lambda *a, **k: _Resp({"error": "invalid_grant"}, status=400))
    r = client.post("/amazon/connect-token", json={"seller_id": SELLER, "refresh_token": "Atzr|wrong"}, headers=headers)
    assert r.status_code == 400
    with SessionLocal() as db:
        assert db.query(AmazonAccount).filter_by(tenant_id=tenant_id).first() is None

    seen = {}

    def ok(url, data=None, **k):
        seen.update(data)
        return _Resp({"access_token": "acc", "expires_in": 3600})

    monkeypatch.setattr(amazon_oauth_service.httpx, "post", ok)
    r = client.post("/amazon/connect-token", json={"seller_id": f" {SELLER} ", "refresh_token": " Atzr|mine "}, headers=headers)
    assert r.status_code == 200 and r.json()["seller_id"] == SELLER and "refresh_token" not in r.json()
    assert seen["grant_type"] == "refresh_token" and seen["refresh_token"] == "Atzr|mine"
    with SessionLocal() as db:
        assert decrypt_token(db.query(AmazonAccount).filter_by(tenant_id=tenant_id).one().refresh_token_encrypted) == "Atzr|mine"


def test_connect_token_without_config_is_503(register, client, monkeypatch):
    headers, _ = _tenant(register, client)
    monkeypatch.setattr(settings, "amazon_lwa_client_secret", "")
    assert client.post("/amazon/connect-token", json={"seller_id": "S", "refresh_token": "t"}, headers=headers).status_code == 503


def test_access_token_is_cached_and_invalid_grant_marks_error(register, client, monkeypatch):
    _, tenant_id = _tenant(register, client)
    account = _account(tenant_id)
    calls = []

    def fake_post(url, data=None, **k):
        calls.append(data["grant_type"])
        return _Resp({"access_token": "tok", "expires_in": 3600})

    monkeypatch.setattr(amazon_oauth_service.httpx, "post", fake_post)
    with SessionLocal() as db:
        row = db.get(AmazonAccount, account.id)
        assert amazon_oauth_service.get_access_token(row, db) == "tok"
        assert amazon_oauth_service.get_access_token(row, db) == "tok"
    assert calls == ["refresh_token"]  # second call came from the cache

    amazon_oauth_service._token_cache.clear()
    monkeypatch.setattr(amazon_oauth_service.httpx, "post", lambda *a, **k: _Resp({"error": "invalid_grant"}, status=400, text="invalid_grant"))
    with SessionLocal() as db:
        row = db.get(AmazonAccount, account.id)
        with pytest.raises(ValueError):
            amazon_oauth_service.get_access_token(row, db)
        assert db.get(AmazonAccount, account.id).status == "ERROR"


def test_disconnect_wipes_token(register, client):
    headers, tenant_id = _tenant(register, client)
    account = _account(tenant_id)
    assert client.post("/amazon/disconnect", params={"account_id": str(account.id)}, headers=headers).status_code == 200
    with SessionLocal() as db:
        row = db.get(AmazonAccount, account.id)
        assert row.status == "DISCONNECTED" and row.refresh_token_encrypted is None
    assert client.get("/amazon/accounts", headers=headers).json() == []


# ------------------------------------------------------------ SP-API call

def test_call_sends_access_token_and_maps_errors(register, client, monkeypatch):
    _, tenant_id = _tenant(register, client)
    account = _account(tenant_id)
    monkeypatch.setattr(amazon_oauth_service, "get_access_token", lambda a, d: "ACCESS")
    seen = {}

    def fake(method, url, **k):
        seen.update(method=method, url=url, headers=k["headers"])
        return _Resp({"ok": 1})

    monkeypatch.setattr(amazon_service.httpx, "request", fake)
    with SessionLocal() as db:
        assert amazon_service.call(account, db, "GET", "/x") == {"ok": 1}
        assert seen["headers"]["x-amz-access-token"] == "ACCESS"
        assert seen["url"] == "https://sellingpartnerapi-eu.amazon.com/x"

        monkeypatch.setattr(amazon_service.httpx, "request", lambda *a, **k: _Resp({}, status=429))
        with pytest.raises(amazon_service.AmazonRateLimited):
            amazon_service.call(account, db, "GET", "/x")
        monkeypatch.setattr(amazon_service.httpx, "request", lambda *a, **k: _Resp({}, status=403, text="Unauthorized"))
        with pytest.raises(ValueError, match="amazon_unauthorized"):
            amazon_service.call(account, db, "GET", "/x")


# ------------------------------------------------------------- listings

def _listing(sku, name="Kabel", price="4.50", qty=7, marketplace="A1PA6795UKMFR9"):
    return {"sku": sku,
            "summaries": [{"marketplaceId": "OTHER", "itemName": "falsch"}, {"marketplaceId": marketplace, "itemName": name, "mainImage": {"link": f"http://img/{sku}.jpg"}}],
            "offers": [{"marketplaceId": marketplace, "offerType": "B2C", "price": {"amount": price, "currencyCode": "EUR"}}],
            "fulfillmentAvailability": [{"fulfillmentChannelCode": "DEFAULT", "quantity": qty}]}


def test_import_products_pages_and_skips_known(register, client, monkeypatch):
    _, tenant_id = _tenant(register, client)
    account = _account(tenant_id)
    pages = [
        {"items": [_listing("A-1"), _listing("KNOWN")], "pagination": {"nextToken": "t2"}},
        {"items": [_listing("B-2", name="Adapter", price="3.99", qty=0), {"sku": "NOMETA"}]},
    ]
    seen = []

    def fake(a, d, method, path, params=None, json=None):
        seen.append((path, dict(params)))
        return pages[len(seen) - 1]

    monkeypatch.setattr(amazon_service, "call", fake)
    with SessionLocal() as db:
        db.add(Product(tenant_id=tenant_id, sku="KNOWN", title="schon da", stock_quantity=1))
        db.commit()
        result = amazon_service.import_products(db.get(AmazonAccount, account.id), tenant_id, db)
        assert result == {"listings_found": 4, "products_created": 3, "already_known": 1}
        a = db.query(Product).filter_by(tenant_id=tenant_id, sku="A-1").one()
        assert a.title == "Kabel" and float(a.selling_price) == 4.5 and a.stock_quantity == 7 and a.image_url == "http://img/A-1.jpg"
        assert db.query(Product).filter_by(tenant_id=tenant_id, sku="KNOWN").one().title == "schon da"
        assert db.query(Product).filter_by(tenant_id=tenant_id, sku="NOMETA").one().title == "NOMETA"
    assert seen[0][0] == f"/listings/2021-08-01/items/{SELLER}" and "pageToken" not in seen[0][1]
    assert seen[1][1]["pageToken"] == "t2" and seen[0][1]["marketplaceIds"] == "A1PA6795UKMFR9"


def test_import_products_endpoint(register, client, monkeypatch):
    headers, tenant_id = _tenant(register, client)
    _account(tenant_id)
    monkeypatch.setattr(amazon_service, "call", lambda *a, **k: {"items": [_listing("E-1")]})
    r = client.post("/amazon/import-products", headers=headers)
    assert r.status_code == 200 and r.json()["products_created"] == 1


# --------------------------------------------------------------- orders

def _item(sku="K-1", qty=1, price="9.99", asin="B0TEST", item_id="1111"):
    return {"orderItemId": item_id, "quantityOrdered": qty,
            "product": {"title": "Artikel", "sellerSku": sku, "asin": asin, "price": {"unitPrice": {"amount": price, "currencyCode": "EUR"}}}}


def _order(number, items=None, status="UNSHIPPED", by="MERCHANT", created=None, address=True):
    return {
        "orderId": number,
        "createdTime": (created or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "fulfillment": {"fulfillmentStatus": status, "fulfilledBy": by},
        "buyer": {"buyerName": "Erika Muster"},
        "recipient": {"deliveryAddress": {"name": "Erika Muster", "addressLine1": "Hauptstr. 1", "addressLine2": "Hof", "city": "Döbeln",
                                          "stateOrRegion": "Sachsen", "postalCode": "04720", "countryCode": "DE", "phone": "0123"}} if address else {},
        "orderItems": items if items is not None else [_item()],
    }


def _sync(tenant_id, monkeypatch, orders, account=None, finance=None):
    account = account or _account(tenant_id)
    seen = []

    def fake(a, d, method, path, params=None, json=None):
        seen.append((path, dict(params or {})))
        if path.startswith("/finances"):
            return finance or {"payload": {"transactions": []}}
        return {"orders": orders}

    monkeypatch.setattr(amazon_service, "call", fake)
    with SessionLocal() as db:
        result = amazon_service.sync_orders(db.get(AmazonAccount, account.id), tenant_id, db)
    return account, result, seen


def test_order_sync_imports_order_with_address_and_decrements_stock(register, client, monkeypatch):
    _, tenant_id = _tenant(register, client)
    with SessionLocal() as db:
        db.add(Product(tenant_id=tenant_id, sku="K-1", title="Kabel", stock_quantity=5, purchase_price=2))
        db.commit()
    account, result, seen = _sync(tenant_id, monkeypatch, [_order("028-1234567-7654321", [_item(qty=2)])])
    assert result["created"] == 1 and result["skipped"] == 0
    with SessionLocal() as db:
        order = db.query(Order).filter_by(tenant_id=tenant_id).one()
        assert order.external_order_ref == "amazon:028-1234567-7654321" and order.status == "new"
        assert order.amazon_account_id == account.id and order.ebay_account_id is None and order.shopify_account_id is None
        assert (order.recipient_name, order.recipient_street1, order.recipient_street2) == ("Erika Muster", "Hauptstr. 1", "Hof")
        assert (order.recipient_zip, order.recipient_state, order.recipient_country_code) == ("04720", "Sachsen", "DE")
        assert order.buyer_username == "Erika Muster"
        item = order.items[0]
        assert item.quantity == 2 and float(item.unit_price) == 9.99 and float(item.purchase_price) == 2.0
        assert db.query(Product).filter_by(tenant_id=tenant_id, sku="K-1").one().stock_quantity == 3
        assert db.query(StockMovement).filter_by(reference="amazon:028-1234567-7654321").one().reason == "amazon_sync"
    path, params = seen[0]
    assert path == "/orders/2026-01-01/orders" and "createdAfter" in params and "lastUpdatedAfter" not in params
    assert params["marketplaceIds"] == "A1PA6795UKMFR9" and "RECIPIENT" in params["includedData"]


def test_later_syncs_only_ask_for_updated_orders(register, client, monkeypatch):
    _, tenant_id = _tenant(register, client)
    account = _account(tenant_id, last_sync_at=datetime.now(timezone.utc) - timedelta(hours=2))
    _, _, seen = _sync(tenant_id, monkeypatch, [], account=account)
    assert "lastUpdatedAfter" in seen[0][1] and "createdAfter" not in seen[0][1]


def test_order_sync_is_idempotent_and_follows_cancellation_and_shipping(register, client, monkeypatch):
    _, tenant_id = _tenant(register, client)
    account, _, _ = _sync(tenant_id, monkeypatch, [_order("A"), _order("B")])
    _sync(tenant_id, monkeypatch, [_order("A", status="CANCELLED"), _order("B", status="SHIPPED")], account=account)
    with SessionLocal() as db:
        rows = {o.external_order_ref: o.status for o in db.query(Order).filter_by(tenant_id=tenant_id)}
    assert rows == {"amazon:A": "canceled", "amazon:B": "shipped"}


def test_pending_orders_are_skipped_cancelled_imported_fba_shipped_without_stock(register, client, monkeypatch):
    _, tenant_id = _tenant(register, client)
    with SessionLocal() as db:
        db.add(Product(tenant_id=tenant_id, sku="P", title="P", stock_quantity=10))
        db.commit()
    now = datetime.now(timezone.utc)
    orders = [
        _order("PEND", [_item("P")], status="PENDING"),
        _order("CANC", [_item("P")], status="CANCELLED"),
        _order("FBA", [_item("P")], status="UNSHIPPED", by="AMAZON"),
        _order("OLD", [_item("P")], created=now - timedelta(days=90)),
        _order("NEW", [_item("P")]),
    ]
    _, result, _ = _sync(tenant_id, monkeypatch, orders)
    assert result["created"] == 4
    with SessionLocal() as db:
        statuses = {o.external_order_ref: o.status for o in db.query(Order).filter_by(tenant_id=tenant_id)}
        assert statuses == {"amazon:CANC": "canceled", "amazon:FBA": "shipped", "amazon:OLD": "new", "amazon:NEW": "new"}
        assert db.query(Product).filter_by(tenant_id=tenant_id, sku="P").one().stock_quantity == 9  # only NEW


def test_order_sync_creates_unknown_product_falls_back_to_asin_and_skips_without_either(register, client, monkeypatch):
    _, tenant_id = _tenant(register, client)
    no_sku = _item(sku=None, asin="B0ASIN", price="3.00")
    none = {"orderItemId": "9", "quantityOrdered": 1, "product": {"title": "Mystery"}}
    _, result, _ = _sync(tenant_id, monkeypatch, [_order("X", [no_sku]), _order("Y", [none])])
    assert result["created"] == 1 and result["products_created"] == 1 and result["skipped"] == 1
    assert "Y" in result["skipped_reasons"][0]
    with SessionLocal() as db:
        assert db.query(Product).filter_by(tenant_id=tenant_id, sku="B0ASIN").one().title == "Artikel"


def test_order_without_pii_roles_is_imported_with_a_note(register, client, monkeypatch):
    _, tenant_id = _tenant(register, client)
    _, result, _ = _sync(tenant_id, monkeypatch, [_order("N", address=False)])
    assert result["created"] == 1 and "keine Adresse" in result["notes"][0]
    with SessionLocal() as db:
        assert db.query(Order).filter_by(tenant_id=tenant_id).one().recipient_street1 is None


def test_rate_limit_is_a_note_not_an_error(register, client, monkeypatch):
    headers, tenant_id = _tenant(register, client)
    _account(tenant_id)

    def limited(*a, **k):
        raise amazon_service.AmazonRateLimited("Amazon: Anfragelimit erreicht")

    monkeypatch.setattr(amazon_service, "call", limited)
    r = client.post("/amazon/sync-orders", headers=headers)
    assert r.status_code == 200 and r.json()["created"] == 0 and "Anfragelimit" in r.json()["fee_notes"][0]


def test_sync_endpoint_and_orders_list_show_amazon_source(register, client, monkeypatch):
    headers, tenant_id = _tenant(register, client)
    _account(tenant_id)
    monkeypatch.setattr(amazon_service, "call", lambda *a, **k: {"orders": [_order("L")]})
    assert client.post("/amazon/sync-orders", headers=headers).json()["created"] == 1
    listed = client.get("/orders", headers=headers).json()
    assert listed[0]["source"] == "amazon" and listed[0]["amazon_account_name"] == SELLER


def test_sync_without_account_is_400(register, client):
    headers, _ = _tenant(register, client)
    assert client.post("/amazon/sync-orders", headers=headers).status_code == 400


# ----------------------------------------------------------------- fees

def _tx(*breakdowns):
    return {"payload": {"transactions": [{"transactionType": "Shipment", "breakdowns": list(breakdowns)}]}}


def test_fees_come_from_the_expenses_breakdown_for_old_enough_orders(register, client, monkeypatch):
    _, tenant_id = _tenant(register, client)
    now = datetime.now(timezone.utc)
    finance = _tx({"breakdownType": "Sales", "breakdownAmount": {"currencyAmount": 20, "currencyCode": "EUR"}},
                  {"breakdownType": "Expenses", "breakdownAmount": {"currencyAmount": -3.15, "currencyCode": "EUR"},
                   "breakdowns": [{"breakdownType": "Commission", "breakdownAmount": {"currencyAmount": -3.15}}]})
    account, _, seen = _sync(tenant_id, monkeypatch, [_order("OLDER", created=now - timedelta(days=5)), _order("FRESH")], finance=finance)
    with SessionLocal() as db:
        fees = {o.external_order_ref: o.ebay_fees for o in db.query(Order).filter_by(tenant_id=tenant_id)}
    assert float(fees["amazon:OLDER"]) == 3.15 and fees["amazon:FRESH"] is None
    lookups = [p for path, p in seen if path.startswith("/finances") and "relatedIdentifierName" in p]
    assert lookups == [{"relatedIdentifierName": "ORDER_ID", "relatedIdentifierValue": "OLDER"}]


def test_no_expenses_breakdown_leaves_fee_empty(register, client, monkeypatch):
    _, tenant_id = _tenant(register, client)
    finance = _tx({"breakdownType": "Sales", "breakdownAmount": {"currencyAmount": 20}})
    _sync(tenant_id, monkeypatch, [_order("O", created=datetime.now(timezone.utc) - timedelta(days=5))], finance=finance)
    with SessionLocal() as db:
        assert db.query(Order).filter_by(tenant_id=tenant_id).one().ebay_fees is None


# ------------------------------------------------------------- shipping

def _amazon_order(tenant_id, account_id, ref="amazon:028-1111111-2222222"):
    with SessionLocal() as db:
        order = Order(tenant_id=tenant_id, external_order_ref=ref, status="new", amazon_account_id=account_id)
        db.add(order)
        db.commit()
        return order.id


def test_ship_confirms_shipment_with_tracking(register, client, monkeypatch):
    headers, tenant_id = _tenant(register, client)
    account = _account(tenant_id)
    order_id = _amazon_order(tenant_id, account.id)
    sent = []

    def fake(a, d, method, path, params=None, json=None):
        sent.append((method, path, json))
        if method == "GET":
            return {"orderId": "028-1111111-2222222", "orderItems": [{"orderItemId": "11", "quantityOrdered": 2}, {"orderItemId": "12", "quantityOrdered": 1}]}
        return {}

    monkeypatch.setattr(amazon_service, "call", fake)
    r = client.post(f"/amazon/orders/{order_id}/ship", json={"tracking_number": "0034 1234-56", "carrier": "DHL"}, headers=headers)
    assert r.status_code == 200 and r.json() == {"status": "shipped", "tracking_number": "0034123456"}
    assert sent[0][:2] == ("GET", "/orders/2026-01-01/orders/028-1111111-2222222")
    method, path, body = sent[1]
    assert (method, path) == ("POST", "/orders/v0/orders/028-1111111-2222222/shipmentConfirmation")
    assert body["marketplaceId"] == "A1PA6795UKMFR9"
    detail = body["packageDetail"]
    assert detail["trackingNumber"] == "0034123456" and detail["carrierCode"] == "DHL" and "carrierName" not in detail
    assert detail["orderItems"] == [{"orderItemId": "11", "quantity": 2}, {"orderItemId": "12", "quantity": 1}]


def test_ship_uses_other_carrier_name_for_non_dhl(register, client, monkeypatch):
    headers, tenant_id = _tenant(register, client)
    account = _account(tenant_id)
    order_id = _amazon_order(tenant_id, account.id)
    sent = []
    monkeypatch.setattr(amazon_service, "call", lambda a, d, m, p, params=None, json=None: sent.append(json) or {"orderItems": [{"orderItemId": "1", "quantityOrdered": 1}]})
    assert client.post(f"/amazon/orders/{order_id}/ship", json={"tracking_number": "H123", "carrier": "HERMES"}, headers=headers).status_code == 200
    assert sent[-1]["packageDetail"]["carrierCode"] == "Other" and sent[-1]["packageDetail"]["carrierName"] == "Hermes"


def test_ship_errors_leave_the_order_untouched(register, client, monkeypatch):
    headers, tenant_id = _tenant(register, client)
    account = _account(tenant_id)
    order_id = _amazon_order(tenant_id, account.id)

    def boom(a, d, method, path, params=None, json=None):
        if method == "GET":
            return {"orderItems": [{"orderItemId": "1", "quantityOrdered": 1}]}
        raise ValueError("amazon_request_failed (400): InvalidInput")

    monkeypatch.setattr(amazon_service, "call", boom)
    r = client.post(f"/amazon/orders/{order_id}/ship", json={"tracking_number": "123", "carrier": "DHL"}, headers=headers)
    assert r.status_code == 502 and "InvalidInput" in r.json()["detail"]
    with SessionLocal() as db:
        assert db.get(Order, order_id).status == "new"

    foreign = _amazon_order(tenant_id, None, ref="21-15411-18885")
    assert client.post(f"/amazon/orders/{foreign}/ship", json={"tracking_number": "123", "carrier": "DHL"}, headers=headers).status_code == 400
    assert client.post(f"/amazon/orders/{order_id}/ship", json={"tracking_number": "1", "carrier": "NOPE"}, headers=headers).status_code == 400
