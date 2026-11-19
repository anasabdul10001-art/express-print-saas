"""Reporting tracking to eBay and marking the order shipped."""

import uuid

from app.database import SessionLocal
from app.models.ebay_account import EbayAccount
from app.models.order import Order
from app.services import ebay_order_service


class _Resp:
    def __init__(self, code=200, body=None):
        self.status_code = code
        self._body = body or {}
        self.text = str(self._body)

    def json(self):
        return self._body


def _setup(register, client, monkeypatch, post_resp):
    headers, _ = register()
    tenant_id = client.get("/auth/me", headers=headers).json()["tenant_id"]
    with SessionLocal() as db:
        db.add(EbayAccount(id=uuid.uuid4(), tenant_id=tenant_id, environment="PRODUCTION", status="CONNECTED"))
        order = Order(tenant_id=tenant_id, external_order_ref="ord-1", status="ready_to_print")
        db.add(order)
        db.commit()
        order_id = str(order.id)
    monkeypatch.setattr(ebay_order_service.ebay_oauth_service, "get_valid_access_token", lambda a, d: "t")
    monkeypatch.setattr(ebay_order_service.httpx, "get",
                        lambda *a, **k: _Resp(200, {"lineItems": [{"lineItemId": "L1", "quantity": 2}]}))
    sent = {}

    def fake_post(url, **kw):
        sent.update(kw["json"])
        return post_resp

    monkeypatch.setattr(ebay_order_service.httpx, "post", fake_post)
    return headers, order_id, sent


def test_ship_sends_tracking_and_marks_shipped(register, client, monkeypatch):
    headers, order_id, sent = _setup(register, client, monkeypatch, _Resp(201))
    r = client.post(f"/ebay/orders/{order_id}/ship",
                    json={"tracking_number": "0034 1234-56", "carrier": "DHL"}, headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "shipped"
    assert sent["trackingNumber"] == "0034123456" and sent["shippingCarrierCode"] == "DHL"
    assert sent["lineItems"] == [{"lineItemId": "L1", "quantity": 2}]
    with SessionLocal() as db:
        assert db.query(Order).filter_by(id=order_id).one().status == "shipped"


def test_ebay_error_keeps_order_unshipped(register, client, monkeypatch):
    headers, order_id, _ = _setup(register, client, monkeypatch, _Resp(400, {"errors": "bad"}))
    r = client.post(f"/ebay/orders/{order_id}/ship",
                    json={"tracking_number": "123", "carrier": "DHL"}, headers=headers)
    assert r.status_code == 502
    with SessionLocal() as db:
        assert db.query(Order).filter_by(id=order_id).one().status == "ready_to_print"


def test_unknown_carrier_rejected(register, client, monkeypatch):
    headers, order_id, _ = _setup(register, client, monkeypatch, _Resp(201))
    r = client.post(f"/ebay/orders/{order_id}/ship",
                    json={"tracking_number": "123", "carrier": "FOO"}, headers=headers)
    assert r.status_code == 400
