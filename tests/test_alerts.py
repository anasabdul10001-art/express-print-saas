"""Smart alerts, and the shipped status they depend on."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.database import SessionLocal
from app.models.ebay_account import EbayAccount
from app.models.order import Order
from app.models.product import Product
from app.services.ebay_order_service import _apply_fulfillment


def _product(client, headers, sku, stock, price=10, cost=1):
    body = {"sku": sku, "title": f"Artikel {sku}", "selling_price": price, "stock_quantity": stock}
    if cost is not None:
        body["purchase_price"] = cost
    return client.post("/products", headers=headers, json=body).json()["id"]


def _order(client, headers, product_id, quantity=1, hours_old=0, ref="R1", **fields):
    order_id = client.post("/orders", headers=headers, json={
        "external_order_ref": ref, "items": [{"product_id": product_id, "quantity": quantity}]}).json()["id"]
    with SessionLocal() as db:
        order = db.get(Order, order_id)
        order.ordered_at = datetime.now(timezone.utc) - timedelta(hours=hours_old)
        for name, value in fields.items():
            setattr(order, name, value)
        db.commit()
    return order_id


def _alerts(client, headers):
    response = client.get("/alerts", headers=headers)
    assert response.status_code == 200, response.text
    return {a["type"]: a for a in response.json()}


def test_nothing_to_report_for_a_new_account(client, register):
    headers, _ = register()
    assert _alerts(client, headers) == {}


def test_requires_login(client):
    assert client.get("/alerts").status_code in (401, 403)


def test_unshipped_only_between_one_day_and_two_weeks(client, register):
    headers, _ = register()
    pid = _product(client, headers, "A", 1000)
    _order(client, headers, pid, ref="fresh", hours_old=2)
    _order(client, headers, pid, ref="late", hours_old=50)
    _order(client, headers, pid, ref="ancient", hours_old=24 * 20)
    _order(client, headers, pid, ref="done", hours_old=50, status="shipped")
    _order(client, headers, pid, ref="refunded", hours_old=50, ebay_payment_status="FULLY_REFUNDED")
    alert = _alerts(client, headers)["unshipped"]
    assert alert["count"] == 1 and alert["items"] == ["late (seit 2 Tg.)"]


def test_out_of_stock_and_low_stock_follow_sales(client, register):
    headers, _ = register()
    empty = _product(client, headers, "EMPTY", 2)
    low = _product(client, headers, "LOW", 13)      # sells 10 in 30 days, 3 left -> about 9 days
    plenty = _product(client, headers, "PLENTY", 100)
    _product(client, headers, "UNSOLD", 0)          # never sold: no alert
    _order(client, headers, empty, 2, ref="1")
    _order(client, headers, low, 10, ref="2")
    _order(client, headers, plenty, 1, ref="3")
    alerts = _alerts(client, headers)
    assert alerts["out_of_stock"]["count"] == 1 and "EMPTY" in alerts["out_of_stock"]["items"][0]
    assert alerts["low_stock"]["items"] == ["Artikel LOW (3 Stück, reicht ca. 9 Tg.)"]


def test_loss_and_missing_cost(client, register):
    headers, _ = register()
    losing = _product(client, headers, "L", 100, price=2, cost=5)
    unknown = _product(client, headers, "U", 100, cost=None)
    _order(client, headers, losing, ref="minus")
    _order(client, headers, unknown, ref="unknown")
    alerts = _alerts(client, headers)
    assert alerts["loss"]["items"] == ["minus: -3,00 €"]
    assert alerts["missing_cost"]["items"] == ["Artikel U"]


def test_broken_and_expiring_connections_and_tenant_isolation(client, register):
    headers, _ = register()
    other, _ = register()
    tenant_id = client.get("/auth/me", headers=headers).json()["tenant_id"]
    with SessionLocal() as db:
        db.add(EbayAccount(tenant_id=tenant_id, ebay_user_id="kaputt", status="ERROR"))
        db.add(EbayAccount(tenant_id=tenant_id, ebay_user_id="bald", status="CONNECTED",
                           refresh_token_expires_at=datetime.now(timezone.utc) + timedelta(days=5, hours=1)))
        db.add(EbayAccount(tenant_id=tenant_id, ebay_user_id="getrennt", status="DISCONNECTED"))
        db.commit()
    alerts = _alerts(client, headers)
    assert alerts["connection_error"]["items"] == ["eBay: kaputt"]
    assert alerts["connection_expiring"]["items"] == ["eBay: bald (noch 5 Tg.)"]
    assert _alerts(client, other) == {}


def test_critical_alerts_come_first(client, register):
    headers, _ = register()
    pid = _product(client, headers, "X", 100, cost=None)
    _order(client, headers, pid, hours_old=48)
    severities = [a["severity"] for a in client.get("/alerts", headers=headers).json()]
    assert severities == ["critical", "info"]


def test_ebay_fulfilled_orders_become_shipped():
    order = Order(status="new")
    _apply_fulfillment(order, {"orderFulfillmentStatus": "IN_PROGRESS"})
    assert order.status == "new"
    _apply_fulfillment(order, {"orderFulfillmentStatus": "FULFILLED"})
    assert order.status == "shipped"
    canceled = Order(status="canceled")
    _apply_fulfillment(canceled, {"orderFulfillmentStatus": "FULFILLED"})
    assert canceled.status == "canceled"
