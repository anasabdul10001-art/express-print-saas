"""Subscription invoices and the emails that come with a paid plan."""
import io
import uuid
from datetime import date, datetime, timezone

import pytest
import stripe
from pypdf import PdfReader
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import engine
from app.services import billing_service
from conftest import set_seller_details

PROFILE = {"billing_name": "Müller Handel e.K.", "billing_street": "Ringstraße 7",
           "billing_zip": "04720", "billing_city": "Döbeln", "billing_country": "de"}


def _sql(query, **values):
    with engine.begin() as conn:
        result = conn.execute(text(query), values)
        return result.fetchall() if result.returns_rows else None


def _pdf_text(data: bytes) -> str:
    return " ".join(" ".join(p.extract_text() for p in PdfReader(io.BytesIO(data)).pages).split())


@pytest.fixture
def mails(monkeypatch):
    """Records the emails instead of sending them; set mails.fail to break the invoice email."""
    class Box(list):
        fail = False

    box = Box()

    def welcome(to, name, plan, sender, url):
        box.append(("welcome", to, plan))

    def invoice(to, number, pdf, sender, cancellation=False):
        if box.fail:
            raise RuntimeError("mail service down")
        box.append(("cancellation" if cancellation else "invoice", to, number, pdf))

    monkeypatch.setattr(billing_service, "send_welcome_email", welcome)
    monkeypatch.setattr(billing_service, "send_invoice_email", invoice)
    monkeypatch.setattr(settings, "operator_small_business", True)
    return box


@pytest.fixture
def customer(client, register):
    headers, payload = register(email="kunde@example.com")
    set_seller_details()
    response = client.put("/billing/profile", headers=headers, json=PROFILE)
    assert response.status_code == 200, response.text
    me = client.get("/auth/me", headers=headers).json()
    _sql("UPDATE tenants SET stripe_customer_id = 'cus_1' WHERE id = :id", id=me["tenant_id"])
    return headers, me


def _pay(client, monkeypatch, stripe_id="in_1", cents=1900, **extra):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_fake")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_fake")
    event = {"type": "invoice.paid", "data": {"object": {
        "id": stripe_id, "customer": "cus_1", "amount_paid": cents, "currency": "eur", **extra}}}
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda *a: event)
    return client.post("/billing/webhooks/stripe", content=b"{}", headers={"stripe-signature": "ok"})


def test_the_invoice_carries_the_required_details(client, customer, mails, monkeypatch):
    start = int(datetime(2026, 11, 28, 10, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2026, 12, 28, 10, tzinfo=timezone.utc).timestamp())
    assert _pay(client, monkeypatch, lines={"data": [{"period": {"start": start, "end": end}}]}).status_code == 200

    assert [m[0] for m in mails] == ["welcome", "invoice"]
    content = _pdf_text(mails[1][3])
    for expected in ("NettoDesk Test", "Teststraße 1", "123/456/7890", "Müller Handel e.K.", "Ringstraße 7",
                     "04720 Döbeln", "Rechnungsdatum", "Software-Abonnement", "28.11.2026 - 27.12.2026",
                     "19.00 EUR", "§ 19 UStG", "Stripe"):
        assert expected in content, expected
    assert mails[1][2] in content


def test_welcome_once_and_one_invoice_per_payment(client, customer, mails, monkeypatch):
    assert _pay(client, monkeypatch).status_code == 200
    assert _pay(client, monkeypatch).status_code == 200  # delivered twice by Stripe
    assert _pay(client, monkeypatch, stripe_id="in_2").status_code == 200  # next month
    assert [m[0] for m in mails] == ["welcome", "invoice", "invoice"]
    numbers = [r[0] for r in _sql("SELECT invoice_number FROM invoices ORDER BY invoice_number")]
    assert len(numbers) == 2 and int(numbers[1][-4:]) == int(numbers[0][-4:]) + 1


def test_a_failed_email_is_sent_by_the_next_delivery(client, customer, mails, monkeypatch):
    mails.fail = True
    assert _pay(client, monkeypatch).status_code == 500
    mails.fail = False
    assert _pay(client, monkeypatch).status_code == 200
    assert [m[0] for m in mails] == ["welcome", "invoice"]
    assert _sql("SELECT count(*) FROM invoices")[0][0] == 1
    assert _sql("SELECT email_sent_at FROM invoices")[0][0] is not None


def test_nothing_charged_means_no_invoice_and_no_email(client, customer, mails, monkeypatch):
    assert _pay(client, monkeypatch, cents=0).status_code == 200
    assert mails == [] and _sql("SELECT count(*) FROM invoices")[0][0] == 0


@pytest.mark.parametrize("broken", [
    "UPDATE tenants SET billing_street = NULL",
    "UPDATE site_settings SET company_tax_id = NULL",
])
def test_missing_details_issue_nothing_and_use_no_number(client, customer, mails, monkeypatch, broken):
    _sql(broken)
    assert _pay(client, monkeypatch).status_code == 500
    assert _sql("SELECT count(*) FROM invoices")[0][0] == 0
    assert _sql("SELECT last_invoice_number FROM site_settings")[0][0] == 0
    assert mails == []


def test_the_issued_pdf_never_changes(client, customer, mails, monkeypatch):
    headers, _ = customer
    _pay(client, monkeypatch)
    _sql("UPDATE site_settings SET company_address = 'Neue Adresse 9'")
    client.put("/billing/profile", headers=headers, json={**PROFILE, "billing_street": "Anderswo 1"})

    listed = client.get("/billing/invoices", headers=headers).json()
    response = client.get(f"/billing/invoices/{listed[0]['id']}/pdf", headers=headers)
    assert response.status_code == 200 and response.content == mails[1][3]


def test_customers_only_see_their_own_invoices(client, register, customer, mails, monkeypatch):
    headers, _ = customer
    _pay(client, monkeypatch)
    invoice_id = client.get("/billing/invoices", headers=headers).json()[0]["id"]
    other, _ = register(email="andere@example.com")
    assert client.get("/billing/invoices", headers=other).json() == []
    assert client.get(f"/billing/invoices/{invoice_id}/pdf", headers=other).status_code == 404
    assert client.get(f"/billing/invoices/{invoice_id}/pdf").status_code in (401, 403)


def test_an_account_with_invoices_cannot_be_deleted(client, customer, mails, monkeypatch):
    _, me = customer
    _pay(client, monkeypatch)
    with pytest.raises(IntegrityError):
        _sql("DELETE FROM tenants WHERE id = :id", id=me["tenant_id"])


def test_cancellation_invoice(client, register, customer, mails, monkeypatch):
    headers, me = customer
    _pay(client, monkeypatch)
    _sql("UPDATE users SET is_superadmin = true WHERE id = :id", id=me["id"])
    original = client.get("/admin/invoices", headers=headers).json()[0]

    response = client.post(f"/admin/invoices/{original['id']}/cancel", headers=headers)
    assert response.status_code == 200, response.text
    storno = response.json()
    assert float(storno["gross_amount"]) == -19 and storno["cancels_id"] == original["id"]
    content = _pdf_text(mails[-1][3])
    assert mails[-1][0] == "cancellation"
    assert "Stornorechnung" in content and original["invoice_number"] in content and "-19.00 EUR" in content

    assert client.post(f"/admin/invoices/{original['id']}/cancel", headers=headers).status_code == 400
    assert client.post(f"/admin/invoices/{storno['id']}/cancel", headers=headers).status_code == 400


@pytest.mark.parametrize("change", [
    {"billing_country": "AT"}, {"billing_zip": "1234"}, {"billing_street": "  "},
    {"billing_name": "شركة"},
])
def test_unusable_invoice_addresses_are_refused(client, customer, change):
    headers, _ = customer
    assert client.put("/billing/profile", headers=headers, json={**PROFILE, **change}).status_code == 400


def test_no_checkout_without_an_invoice_address(client, register, monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_fake")
    headers, _ = register(email="ohne@example.com")
    me = client.get("/auth/me", headers=headers).json()
    _sql("UPDATE users SET is_superadmin = true WHERE id = :id", id=me["id"])
    plan = client.post("/admin/plans", headers=headers, json={
        "name": "Plus", "price_monthly": 19, "stripe_price_id_monthly": "price_1"}).json()
    set_seller_details()
    response = client.post("/billing/checkout-session", headers=headers, json={"plan_id": plan["id"]})
    assert response.status_code == 400 and "Rechnungsadresse" in response.json()["detail"]


@pytest.mark.parametrize("start, end", [
    (date(2026, 11, 28), date(2026, 12, 27)), (date(2026, 12, 15), date(2027, 1, 14)),
    (date(2027, 1, 31), date(2027, 2, 28)), (date(2027, 3, 1), date(2027, 3, 31)),
])
def test_month_period(start, end):
    assert billing_service.month_period(start) == (start, end)
