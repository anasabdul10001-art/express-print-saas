"""eBay's Marketplace Account Deletion endpoint."""

import base64
import hashlib
import json
import uuid

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.config import settings
from app.database import SessionLocal
from app.models.ebay_account import EbayAccount
from app.models.order import Order
from app.models.print_agent import PrintAgent
from app.models.print_job import PrintJob
from app.models.product import Product
from app.models.stock_movement import StockMovement
from app.models.user import User
from app.services import ebay_deletion_service, ebay_order_service
from app.services.ebay_deletion_service import deleted_order_ref

TOKEN = "t" * 40
PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
PUBLIC_PEM = PRIVATE_KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
).decode()


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "ebay_deletion_verification_token", TOKEN)
    monkeypatch.setattr(ebay_deletion_service, "_public_key", lambda key_id: PUBLIC_PEM)


def _signed(payload, key=PRIVATE_KEY):
    body = json.dumps(payload).encode()
    signature = key.sign(body, ec.ECDSA(hashes.SHA1()))
    header = base64.b64encode(
        json.dumps({"alg": "ECDSA", "kid": "key-1", "signature": base64.b64encode(signature).decode()}).encode()
    ).decode()
    return body, {"X-EBAY-SIGNATURE": header, "Content-Type": "application/json"}


def _notification(username="buyer1", user_id="immutable-1"):
    return {
        "metadata": {"topic": "MARKETPLACE_ACCOUNT_DELETION", "schemaVersion": "1.0", "deprecated": False},
        "notification": {
            "notificationId": "n1",
            "eventDate": "2026-11-04T00:00:00.000Z",
            "publishDate": "2026-11-04T00:00:00.000Z",
            "publishAttemptCount": 1,
            "data": {"username": username, "userId": user_id, "eiasToken": "eias"},
        },
    }


def _tenant_id(register):
    _, payload = register()
    with SessionLocal() as db:
        return db.query(User).filter(User.email == payload["email"]).first().tenant_id


def _add_order(tenant_id, buyer="buyer1", ref="ebay-order-1"):
    with SessionLocal() as db:
        order = Order(
            tenant_id=tenant_id,
            external_order_ref=ref,
            buyer_username=buyer,
            recipient_name="Max Mustermann",
            recipient_street1="Hauptstr. 1",
            recipient_city="Berlin",
            recipient_zip="10115",
            recipient_country_code="DE",
            recipient_phone="123",
            tracking_number="TRACK",
        )
        db.add(order)
        db.commit()
        return order.id


def test_challenge_response_uses_ebays_hash_order(client):
    response = client.get("/ebay/account-deletion", params={"challenge_code": "abc"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    expected = hashlib.sha256(("abc" + TOKEN + settings.ebay_deletion_endpoint).encode()).hexdigest()
    assert response.json() == {"challengeResponse": expected}


def test_missing_or_forged_signature_deletes_nothing(client, register):
    order_id = _add_order(_tenant_id(register))
    body, headers = _signed(_notification(), key=ec.generate_private_key(ec.SECP256R1()))

    assert client.post("/ebay/account-deletion", content=body, headers=headers).status_code == 412
    assert client.post("/ebay/account-deletion", content=body).status_code == 412

    with SessionLocal() as db:
        assert db.get(Order, order_id).recipient_name == "Max Mustermann"


def test_buyer_deletion_wipes_their_data_only(client, register):
    tenant_id = _tenant_id(register)
    order_id = _add_order(tenant_id)
    other_id = _add_order(tenant_id, buyer="someone-else", ref="ebay-order-2")

    with SessionLocal() as db:
        product = Product(tenant_id=tenant_id, sku="S", title="T", purchase_price=1, selling_price=2)
        agent = PrintAgent(tenant_id=tenant_id, name="A", api_key_hash="x")
        db.add_all([product, agent])
        db.flush()
        db.add(StockMovement(tenant_id=tenant_id, product_id=product.id, quantity_change=-1, reason="ebay_sync", reference="ebay-order-1"))
        job = PrintJob(tenant_id=tenant_id, print_agent_id=agent.id, order_id=order_id, label_pdf_url="u", label_pdf_data=b"pdf")
        db.add(job)
        db.commit()
        job_id = job.id

    body, headers = _signed(_notification())
    for _ in range(2):  # eBay may resend
        assert client.post("/ebay/account-deletion", content=body, headers=headers).status_code == 204

    with SessionLocal() as db:
        order = db.get(Order, order_id)
        assert order.buyer_username is None
        assert order.recipient_name is None
        assert order.recipient_street1 is None
        assert order.recipient_phone is None
        assert order.tracking_number is None
        assert order.external_order_ref == deleted_order_ref("ebay-order-1")
        assert db.get(PrintJob, job_id).label_pdf_data is None
        assert db.query(StockMovement).filter_by(reference="ebay-order-1").count() == 0
        assert db.query(StockMovement).filter_by(reference=deleted_order_ref("ebay-order-1")).count() == 1
        assert db.get(Order, other_id).recipient_name == "Max Mustermann"


def test_seller_deletion_removes_tokens_and_imported_orders(client, register):
    tenant_id = _tenant_id(register)
    order_id = _add_order(tenant_id, buyer="a-buyer")
    with SessionLocal() as db:
        db.add(EbayAccount(tenant_id=tenant_id, ebay_user_id="seller1", ebay_immutable_id="seller-id", status="CONNECTED",
                           access_token_encrypted="a", refresh_token_encrypted="r"))
        db.commit()

    body, headers = _signed(_notification(username=None, user_id="seller-id"))
    assert client.post("/ebay/account-deletion", content=body, headers=headers).status_code == 204

    with SessionLocal() as db:
        account = db.query(EbayAccount).filter_by(tenant_id=tenant_id).first()
        assert account.status == "DISCONNECTED"
        assert account.ebay_user_id is None
        assert account.access_token_encrypted is None
        assert account.refresh_token_encrypted is None
        assert db.get(Order, order_id).recipient_name is None


def test_unknown_user_and_other_topics_are_acknowledged(client, register):
    order_id = _add_order(_tenant_id(register))
    body, headers = _signed(_notification(username="nobody", user_id="nobody-id"))
    assert client.post("/ebay/account-deletion", content=body, headers=headers).status_code == 204

    other = _notification()
    other["metadata"]["topic"] = "SOMETHING_ELSE"
    body, headers = _signed(other)
    assert client.post("/ebay/account-deletion", content=body, headers=headers).status_code == 204

    with SessionLocal() as db:
        assert db.get(Order, order_id).recipient_name == "Max Mustermann"


def test_unreachable_public_key_asks_ebay_to_retry(client, monkeypatch):
    def unavailable(key_id):
        raise ebay_deletion_service.PublicKeyUnavailable("down")

    monkeypatch.setattr(ebay_deletion_service, "_public_key", unavailable)
    body, headers = _signed(_notification())
    assert client.post("/ebay/account-deletion", content=body, headers=headers).status_code == 503


class _FakeOrdersResponse:
    status_code = 200
    text = ""

    def __init__(self, orders):
        self._orders = orders

    def json(self):
        return {"orders": self._orders}


def test_sync_records_buyer_backfills_and_never_reimports_deleted(register, monkeypatch):
    tenant_id = _tenant_id(register)
    legacy_id = _add_order(tenant_id, buyer=None, ref="legacy")
    deleted_id = _add_order(tenant_id, buyer=None, ref=deleted_order_ref("gone"))

    with SessionLocal() as db:
        db.add(Product(tenant_id=tenant_id, sku="S", title="T", purchase_price=1, selling_price=2))
        account = EbayAccount(id=uuid.uuid4(), tenant_id=tenant_id, environment="PRODUCTION", status="CONNECTED")
        db.add(account)
        db.commit()
        orders = [
            {"orderId": "new", "sellerId": "seller1", "buyer": {"username": "b-new"}, "lineItems": [{"sku": "S", "quantity": 1}]},
            {"orderId": "legacy", "buyer": {"username": "b-legacy"}, "lineItems": []},
            {"orderId": "gone", "buyer": {"username": "b-gone"}, "lineItems": [{"sku": "S", "quantity": 1}]},
        ]
        monkeypatch.setattr(ebay_order_service.ebay_oauth_service, "get_valid_access_token", lambda a, d: "t")
        monkeypatch.setattr(ebay_order_service.httpx, "get", lambda *a, **k: _FakeOrdersResponse(orders))
        result = ebay_order_service.sync_orders(account, tenant_id, db)

    assert result["created"] == 1
    with SessionLocal() as db:
        assert db.query(Order).filter_by(external_order_ref="new").first().buyer_username == "b-new"
        assert db.get(Order, legacy_id).buyer_username == "b-legacy"
        assert db.get(Order, deleted_id).buyer_username is None
