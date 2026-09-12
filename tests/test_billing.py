"""
Stripe subscription billing (see app/routers/billing.py and
app/services/billing_service.py). No live Stripe test-mode key was
available when this was built, so every test here mocks the `stripe`
module directly rather than hitting Stripe's real API - these tests prove
this app's own logic (request validation, DB writes, the invoice/affiliate
path) is correct, but do NOT prove Stripe integration itself works
end-to-end. See app/routers/billing.py's module docstring.
"""

import types
import uuid
from datetime import datetime, timedelta, timezone

import stripe
from sqlalchemy import create_engine, text

from app.config import settings


def _make_superadmin(headers, client):
    me = client.get("/auth/me", headers=headers).json()
    engine = create_engine(settings.database_url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE users SET is_superadmin = true WHERE id = :id"), {"id": me["id"]})


def _set_billing_details(client, admin_headers):
    response = client.patch("/admin/settings", headers=admin_headers, json={
        "company_legal_name": "ShipSync Test GmbH",
        "company_address": "Teststraße 1, 10115 Berlin",
        "company_tax_id": "DE123456789",
        "company_email": "billing@example.com",
    })
    assert response.status_code == 200, response.text


def _create_plan(client, admin_headers, name="Stripe Test Plan", stripe_price_id="price_test123"):
    response = client.post("/admin/plans", headers=admin_headers, json={
        "name": name, "price_monthly": 29, "stripe_price_id_monthly": stripe_price_id,
    })
    assert response.status_code == 201, response.text
    return response.json()


def _configure_stripe(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_fake")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_fake")


def test_checkout_session_without_stripe_configured_returns_503(client, register):
    headers, _ = register(email="nostripe@example.com")
    response = client.post("/billing/checkout-session", headers=headers, json={"plan_id": str(uuid.uuid4())})
    assert response.status_code == 503


def test_checkout_session_requires_known_plan(client, register, monkeypatch):
    _configure_stripe(monkeypatch)
    headers, _ = register(email="unknownplan@example.com")

    response = client.post("/billing/checkout-session", headers=headers, json={"plan_id": str(uuid.uuid4())})
    assert response.status_code == 404


def test_checkout_session_requires_plan_with_stripe_price(client, register, monkeypatch):
    _configure_stripe(monkeypatch)
    admin_headers, _ = register(email="planadmin1@example.com")
    _make_superadmin(admin_headers, client)

    plan = client.post("/admin/plans", headers=admin_headers, json={
        "name": "No Stripe Price Plan", "price_monthly": 19,
    }).json()

    headers, _ = register(email="noprice@example.com")
    response = client.post("/billing/checkout-session", headers=headers, json={"plan_id": plan["id"]})
    assert response.status_code == 400


def test_checkout_session_creates_customer_and_returns_url(client, register, monkeypatch):
    _configure_stripe(monkeypatch)
    admin_headers, _ = register(email="planadmin2@example.com")
    _make_superadmin(admin_headers, client)
    plan = _create_plan(client, admin_headers)

    created_customers = []

    def fake_customer_create(**kwargs):
        created_customers.append(kwargs)
        return types.SimpleNamespace(id="cus_fake123")

    def fake_session_create(**kwargs):
        assert kwargs["customer"] == "cus_fake123"
        assert kwargs["line_items"][0]["price"] == "price_test123"
        return types.SimpleNamespace(url="https://checkout.stripe.com/fake-session")

    monkeypatch.setattr(stripe.Customer, "create", fake_customer_create)
    monkeypatch.setattr(stripe.checkout.Session, "create", fake_session_create)

    headers, _ = register(email="checkoutuser@example.com")
    response = client.post("/billing/checkout-session", headers=headers, json={"plan_id": plan["id"]})
    assert response.status_code == 200, response.text
    assert response.json()["url"] == "https://checkout.stripe.com/fake-session"
    assert len(created_customers) == 1

    me = client.get("/tenants/me", headers=headers).json()
    engine = create_engine(settings.database_url)
    with engine.begin() as conn:
        row = conn.execute(text("SELECT stripe_customer_id FROM tenants WHERE id = :id"), {"id": me["id"]}).first()
    assert row.stripe_customer_id == "cus_fake123"


def test_checkout_session_reuses_existing_stripe_customer(client, register, monkeypatch):
    _configure_stripe(monkeypatch)
    admin_headers, _ = register(email="planadmin3@example.com")
    _make_superadmin(admin_headers, client)
    plan = _create_plan(client, admin_headers)

    headers, _ = register(email="reuseuser@example.com")
    me = client.get("/auth/me", headers=headers).json()
    engine = create_engine(settings.database_url)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE tenants SET stripe_customer_id = 'cus_existing' WHERE id = :id"),
            {"id": me["tenant_id"]},
        )

    customer_create_calls = []
    monkeypatch.setattr(stripe.Customer, "create", lambda **kw: customer_create_calls.append(kw))
    monkeypatch.setattr(stripe.checkout.Session, "create", lambda **kw: types.SimpleNamespace(url="https://checkout.stripe.com/reused"))

    response = client.post("/billing/checkout-session", headers=headers, json={"plan_id": plan["id"]})
    assert response.status_code == 200, response.text
    assert len(customer_create_calls) == 0  # never created a new customer


def test_checkout_session_allowed_even_when_trial_expired(client, register, monkeypatch):
    """Paying is the one thing a trial-expired tenant must still be able to
    do - see app/core/deps.py's get_current_user_ignoring_trial."""
    _configure_stripe(monkeypatch)
    admin_headers, _ = register(email="planadmin4@example.com")
    _make_superadmin(admin_headers, client)
    plan = _create_plan(client, admin_headers)

    headers, _ = register(email="expireduser@example.com")
    me = client.get("/auth/me", headers=headers).json()
    engine = create_engine(settings.database_url)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE tenants SET trial_ends_at = :past WHERE id = :id"),
            {"past": datetime.now(timezone.utc) - timedelta(days=1), "id": me["tenant_id"]},
        )

    # Confirm the trial really is blocking normal endpoints first.
    blocked = client.get("/tenants/me", headers=headers)
    assert blocked.status_code == 402

    monkeypatch.setattr(stripe.Customer, "create", lambda **kw: types.SimpleNamespace(id="cus_expired"))
    monkeypatch.setattr(stripe.checkout.Session, "create", lambda **kw: types.SimpleNamespace(url="https://checkout.stripe.com/expired-trial"))

    response = client.post("/billing/checkout-session", headers=headers, json={"plan_id": plan["id"]})
    assert response.status_code == 200, response.text


def test_portal_session_requires_stripe_customer(client, register, monkeypatch):
    _configure_stripe(monkeypatch)
    headers, _ = register(email="noportal@example.com")

    response = client.post("/billing/portal-session", headers=headers)
    assert response.status_code == 400


def test_portal_session_returns_url(client, register, monkeypatch):
    _configure_stripe(monkeypatch)
    headers, _ = register(email="portaluser@example.com")
    me = client.get("/auth/me", headers=headers).json()
    engine = create_engine(settings.database_url)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE tenants SET stripe_customer_id = 'cus_portal' WHERE id = :id"),
            {"id": me["tenant_id"]},
        )

    monkeypatch.setattr(
        stripe.billing_portal.Session, "create",
        lambda **kw: types.SimpleNamespace(url="https://billing.stripe.com/fake-portal"),
    )

    response = client.post("/billing/portal-session", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["url"] == "https://billing.stripe.com/fake-portal"


def test_webhook_requires_stripe_configured(client):
    response = client.post("/billing/webhooks/stripe", content=b"{}", headers={"stripe-signature": "x"})
    assert response.status_code == 503


def test_webhook_rejects_invalid_signature(client, monkeypatch):
    _configure_stripe(monkeypatch)

    def fake_construct_event(payload, sig_header, secret):
        raise stripe.error.SignatureVerificationError("bad signature", sig_header)

    monkeypatch.setattr(stripe.Webhook, "construct_event", fake_construct_event)

    response = client.post("/billing/webhooks/stripe", content=b"{}", headers={"stripe-signature": "bad"})
    assert response.status_code == 400


def test_webhook_checkout_completed_updates_tenant(client, register, monkeypatch):
    _configure_stripe(monkeypatch)
    headers, _ = register(email="webhookuser1@example.com")
    me = client.get("/auth/me", headers=headers).json()
    tenant_id = me["tenant_id"]

    fake_event = {
        "type": "checkout.session.completed",
        "data": {"object": {
            "client_reference_id": tenant_id,
            "customer": "cus_webhook1",
            "subscription": "sub_webhook1",
        }},
    }
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda payload, sig_header, secret: fake_event)

    response = client.post("/billing/webhooks/stripe", content=b"{}", headers={"stripe-signature": "valid"})
    assert response.status_code == 200

    engine = create_engine(settings.database_url)
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT stripe_customer_id, stripe_subscription_id, subscription_status, trial_ends_at FROM tenants WHERE id = :id"),
            {"id": tenant_id},
        ).first()
    assert row.stripe_customer_id == "cus_webhook1"
    assert row.stripe_subscription_id == "sub_webhook1"
    assert row.subscription_status == "active"
    assert row.trial_ends_at is None


def test_webhook_invoice_paid_creates_invoice_and_awards_commission(client, register, monkeypatch):
    _configure_stripe(monkeypatch)
    admin_headers, _ = register(email="webhookadmin@example.com")
    _make_superadmin(admin_headers, client)
    _set_billing_details(client, admin_headers)

    affiliate = client.post("/admin/affiliates", headers=admin_headers, json={
        "name": "Webhook Referrer", "commission_type": "PERCENTAGE_RECURRING", "commission_value": 10,
    }).json()

    referred = client.post("/auth/register", json={
        "email": "webhookreferred@example.com", "password": "testpass123", "company_name": "Webhook Referred Co",
        "early_service_consent": True, "referral_code": affiliate["referral_code"],
    })
    assert referred.status_code == 201
    tenants = client.get("/admin/tenants", headers=admin_headers).json()
    tenant = next(t for t in tenants if t["company_name"] == "Webhook Referred Co")

    engine = create_engine(settings.database_url)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE tenants SET stripe_customer_id = 'cus_invoicepaid' WHERE id = :id"),
            {"id": tenant["id"]},
        )
        invoices_before = conn.execute(text("SELECT COUNT(*) FROM invoices")).scalar()

    # 34.51 gross ~= 29.00 net at 19% VAT, in cents as Stripe would send it.
    fake_event = {
        "type": "invoice.paid",
        "data": {"object": {
            "customer": "cus_invoicepaid",
            "amount_paid": 3451,
            "currency": "eur",
        }},
    }
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda payload, sig_header, secret: fake_event)

    response = client.post("/billing/webhooks/stripe", content=b"{}", headers={"stripe-signature": "valid"})
    assert response.status_code == 200

    with engine.begin() as conn:
        invoices_after = conn.execute(text("SELECT COUNT(*) FROM invoices")).scalar()
        invoice_row = conn.execute(
            text("SELECT net_amount, gross_amount FROM invoices WHERE tenant_id = :id"), {"id": tenant["id"]}
        ).first()
    assert invoices_after == invoices_before + 1
    assert float(invoice_row.gross_amount) == 34.51
    assert float(invoice_row.net_amount) == 29.00

    affiliates_after = client.get("/admin/affiliates", headers=admin_headers).json()
    affiliate_after = next(a for a in affiliates_after if a["id"] == affiliate["id"])
    assert float(affiliate_after["balance_owed"]) == round(34.51 * 0.10, 2)


def test_webhook_invoice_paid_unknown_customer_is_ignored(client, monkeypatch):
    _configure_stripe(monkeypatch)
    engine = create_engine(settings.database_url)
    with engine.begin() as conn:
        invoices_before = conn.execute(text("SELECT COUNT(*) FROM invoices")).scalar()

    fake_event = {
        "type": "invoice.paid",
        "data": {"object": {"customer": "cus_does_not_exist", "amount_paid": 1000, "currency": "eur"}},
    }
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda payload, sig_header, secret: fake_event)

    response = client.post("/billing/webhooks/stripe", content=b"{}", headers={"stripe-signature": "valid"})
    assert response.status_code == 200

    with engine.begin() as conn:
        invoices_after = conn.execute(text("SELECT COUNT(*) FROM invoices")).scalar()
    assert invoices_after == invoices_before
