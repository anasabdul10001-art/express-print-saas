"""Automatic removal of old personal data, and the download of the account's data."""
import io
import json
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.rate_limit import limiter
from app.database import SessionLocal
from app.models.order import Order
from app.models.password_reset_token import PasswordResetToken
from app.models.print_agent import PrintAgent
from app.models.print_job import PrintJob
from app.models.tenant import Tenant
from app.models.user import User
from app.services import retention_service

NOW = datetime(2030, 6, 15, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_limit():
    limiter.reset()


def _tenant(client, headers):
    return uuid.UUID(client.get("/auth/me", headers=headers).json()["tenant_id"])


def _order(tenant_id, ref, months_old, status="shipped"):
    with SessionLocal() as db:
        order = Order(tenant_id=tenant_id, external_order_ref=ref, status=status, buyer_username="buyer-x",
                      recipient_name="Erika Beispiel", recipient_street1="Weg 1", recipient_zip="01234",
                      recipient_city="Ort", recipient_phone="0123", tracking_number="T1",
                      ebay_buyer_total=Decimal("9.99"),
                      ordered_at=retention_service.months_before(NOW, months_old) - timedelta(days=1))
        db.add(order)
        db.commit()
        return order.id


def _job(tenant_id, order_id, status="printed", days_old=1):
    with SessionLocal() as db:
        agent = db.query(PrintAgent).filter(PrintAgent.tenant_id == tenant_id).first()
        if agent is None:
            agent = PrintAgent(tenant_id=tenant_id, name="a", api_key_hash=uuid.uuid4().hex + uuid.uuid4().hex)
            db.add(agent)
            db.flush()
        job = PrintJob(tenant_id=tenant_id, print_agent_id=agent.id, order_id=order_id, status=status,
                       label_pdf_url="https://example.com/l.pdf", label_pdf_data=b"label",
                       created_at=NOW - timedelta(days=days_old))
        db.add(job)
        db.commit()
        return job.id


def _get(model, ident):
    with SessionLocal() as db:
        return db.get(model, ident)


def _run():
    with SessionLocal() as db:
        return retention_service.run(db, NOW)


def test_old_finished_orders_lose_the_buyer_but_keep_the_money(client, register):
    headers, _ = register()
    tid = _tenant(client, headers)
    old = _order(tid, "old", 37)
    young = _order(tid, "young", 35)
    open_order = _order(tid, "open", 48, status="new")
    job = _job(tid, old)
    assert _run()["orders"] == 1
    gone = _get(Order, old)
    assert all(getattr(gone, f) is None for f in retention_service.BUYER_FIELDS)
    assert gone.ebay_buyer_total == Decimal("9.99") and gone.external_order_ref == "old"
    assert _get(PrintJob, job).label_pdf_data is None
    assert _get(Order, young).recipient_name == "Erika Beispiel"
    assert _get(Order, open_order).recipient_name == "Erika Beispiel"  # not finished yet
    assert _run() == {"orders": 0, "print_files": 0, "tokens": 0}


def test_the_period_is_each_accounts_own_choice(client, register):
    short, _ = register()
    long, _ = register()
    a, b = _order(_tenant(client, short), "a", 7), _order(_tenant(client, long), "b", 7)
    assert client.put("/account/retention", headers=short, json={"months": 6}).status_code == 200
    assert _run()["orders"] == 1
    assert _get(Order, a).recipient_name is None and _get(Order, b).recipient_name is not None


def test_only_the_offered_periods_and_never_earlier_through_a_bad_value(client, register):
    headers, _ = register()
    for months in (0, 1, 5, 7, 120, -6):
        assert client.put("/account/retention", headers=headers, json={"months": months}).status_code == 400
    assert client.get("/account/retention", headers=headers).json() == {"months": 36, "choices": [6, 12, 24, 36]}
    tid = _tenant(client, headers)
    kept = _order(tid, "x", 12)
    with SessionLocal() as db:
        db.query(Tenant).filter(Tenant.id == tid).update({"buyer_data_retention_months": 0})
        db.commit()
    assert _run()["orders"] == 0 and _get(Order, kept).recipient_name is not None


def test_old_print_files_and_expired_tokens_go(client, register):
    headers, _ = register()
    tid = _tenant(client, headers)
    old = _job(tid, None, "printed", 91)
    waiting = _job(tid, None, "pending", 200)
    fresh = _job(tid, None, "printed", 10)
    with SessionLocal() as db:
        user = db.query(User).filter(User.tenant_id == tid).one()
        db.add(PasswordResetToken(user_id=user.id, token_hash="a" * 64, expires_at=NOW - timedelta(minutes=1)))
        db.add(PasswordResetToken(user_id=user.id, token_hash="b" * 64, expires_at=NOW + timedelta(hours=1)))
        db.commit()
    result = _run()
    assert (result["print_files"], result["tokens"]) == (1, 1)
    assert _get(PrintJob, old).label_pdf_data is None
    assert _get(PrintJob, waiting).label_pdf_data == b"label" == _get(PrintJob, fresh).label_pdf_data


def test_months_are_counted_by_the_calendar():
    assert retention_service.months_before(datetime(2028, 8, 31), 6) == datetime(2028, 2, 29)
    assert retention_service.months_before(datetime(2027, 1, 15), 12) == datetime(2026, 1, 15)


def test_the_export_has_the_own_data_without_secrets(client, register):
    headers, payload = register()
    tid = _tenant(client, headers)
    _order(tid, "mine", 1)
    _job(tid, None)
    client.post("/products", headers=headers, json={"sku": "S1", "title": "Mein Produkt", "selling_price": 2})
    client.put("/accounting/eks/profile", headers=headers,
               json={"consent": True, "details": {"city": "Musterstadt"}})
    other, other_payload = register()
    _order(_tenant(client, other), "theirs", 1)

    r = client.get("/account/export", headers=headers)
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    archive = zipfile.ZipFile(io.BytesIO(r.content))
    raw = archive.read("konto.json").decode("utf-8")
    data = json.loads(raw)
    assert [o["external_order_ref"] for o in data["orders"]] == ["mine"]
    assert data["products"][0]["title"] == "Mein Produkt" and data["eks_angaben"]["city"] == "Musterstadt"
    assert data["users"][0]["email"] == payload["email"]
    assert other_payload["email"] not in raw and "theirs" not in raw
    for table in data.values():
        if isinstance(table, list):
            for row in table:
                assert not [k for k in row if any(m in k for m in ("password", "_hash", "_encrypted", "token", "jwe", "file_data", "pdf_data"))], row.keys()


def test_login_and_owner_are_required(client, register):
    assert client.get("/account/export").status_code in (401, 403, 422)
    headers, _ = register()
    with SessionLocal() as db:
        db.query(User).filter(User.tenant_id == _tenant(client, headers)).update({"role": "staff"})
        db.commit()
    assert client.get("/account/export", headers=headers).status_code == 403
    assert client.put("/account/retention", headers=headers, json={"months": 6}).status_code == 403
