"""
Affiliate/referral program: referral capture at signup, admin CRUD, and
commission awarding on confirm-payment (see app/routers/admin.py's
_award_affiliate_commission and app/routers/auth.py's register()).

confirm_payment always returns 502 in this test environment (no
RESEND_API_KEY configured - see app/services/email_service.py), same as it
would in any environment without Resend set up. That's expected here: the
invoice AND the commission are both already committed by the time the
email step runs (see app/routers/admin.py), so these tests check the
commission/balance directly rather than the confirm_payment response code.
"""

from sqlalchemy import create_engine, text

from app.config import settings

PAYMENT_METHOD_NOT_NEEDED = None  # confirm-payment doesn't require one


def _make_superadmin(headers, client):
    me = client.get("/auth/me", headers=headers).json()
    engine = create_engine(settings.database_url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE users SET is_superadmin = true WHERE id = :id"), {"id": me["id"]})
    return me


def _set_billing_details(client, admin_headers):
    response = client.patch("/admin/settings", headers=admin_headers, json={
        "company_legal_name": "ShipSync Test GmbH",
        "company_address": "Teststraße 1, 10115 Berlin",
        "company_tax_id": "DE123456789",
        "company_email": "billing@example.com",
    })
    assert response.status_code == 200, response.text


def _confirm_payment(client, admin_headers, tenant_id, amount=None):
    payload = {}
    if amount is not None:
        payload["amount"] = amount
    return client.post(f"/admin/tenants/{tenant_id}/confirm-payment", headers=admin_headers, json=payload)


def test_non_superadmin_cannot_manage_affiliates(client, register):
    headers, _ = register()
    response = client.post("/admin/affiliates", headers=headers, json={
        "name": "Test Affiliate", "commission_type": "FLAT_ONE_TIME", "commission_value": 20,
    })
    assert response.status_code == 403


def test_create_affiliate_generates_unique_referral_code(client, register):
    admin_headers, _ = register(email="admin1@example.com")
    _make_superadmin(admin_headers, client)

    response = client.post("/admin/affiliates", headers=admin_headers, json={
        "name": "Max Mustermann", "email": "max@example.com",
        "commission_type": "PERCENTAGE_RECURRING", "commission_value": 15,
    })
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["referral_code"]
    assert body["status"] == "ACTIVE"
    assert body["referral_count"] == 0
    assert float(body["balance_owed"]) == 0


def test_register_with_valid_referral_code_creates_referral(client, register):
    admin_headers, _ = register(email="admin2@example.com")
    _make_superadmin(admin_headers, client)

    affiliate = client.post("/admin/affiliates", headers=admin_headers, json={
        "name": "Referrer", "commission_type": "FLAT_ONE_TIME", "commission_value": 25,
    }).json()

    response = client.post("/auth/register", json={
        "email": "referred@example.com", "password": "testpass123",
        "company_name": "Referred Co", "country_code": "DE",
        "referral_code": affiliate["referral_code"], "early_service_consent": True,
    })
    assert response.status_code == 201

    affiliates = client.get("/admin/affiliates", headers=admin_headers).json()
    updated = next(a for a in affiliates if a["id"] == affiliate["id"])
    assert updated["referral_count"] == 1


def test_register_with_unknown_referral_code_is_ignored(client):
    response = client.post("/auth/register", json={
        "email": "noref@example.com", "password": "testpass123",
        "company_name": "No Ref Co", "country_code": "DE",
        "referral_code": "DOES-NOT-EXIST", "early_service_consent": True,
    })
    assert response.status_code == 201  # never blocks signup over a bad code


def test_flat_bonus_awarded_once_not_on_second_payment(client, register):
    admin_headers, _ = register(email="admin3@example.com")
    admin = _make_superadmin(admin_headers, client)
    _set_billing_details(client, admin_headers)

    affiliate = client.post("/admin/affiliates", headers=admin_headers, json={
        "name": "Flat Referrer", "commission_type": "FLAT_ONE_TIME", "commission_value": 20,
    }).json()

    tenant_headers, _ = register(email="flatref@example.com")
    client.post("/auth/register", json={
        "email": "flatref2@example.com", "password": "testpass123",
        "company_name": "Flat Ref Tenant", "country_code": "DE",
        "referral_code": affiliate["referral_code"], "early_service_consent": True,
    })
    tenant_id = client.get("/admin/tenants", headers=admin_headers).json()
    referred_tenant = next(t for t in tenant_id if t["company_name"] == "Flat Ref Tenant")

    _confirm_payment(client, admin_headers, referred_tenant["id"], amount=29)
    after_first = client.get("/admin/affiliates", headers=admin_headers).json()
    affiliate_after_first = next(a for a in after_first if a["id"] == affiliate["id"])
    assert float(affiliate_after_first["balance_owed"]) == 20
    assert float(affiliate_after_first["total_earned"]) == 20

    _confirm_payment(client, admin_headers, referred_tenant["id"], amount=29)
    after_second = client.get("/admin/affiliates", headers=admin_headers).json()
    affiliate_after_second = next(a for a in after_second if a["id"] == affiliate["id"])
    assert float(affiliate_after_second["balance_owed"]) == 20  # unchanged - bonus is one-time only
    assert float(affiliate_after_second["total_earned"]) == 20


def test_percentage_commission_awarded_every_payment(client, register):
    admin_headers, _ = register(email="admin4@example.com")
    _make_superadmin(admin_headers, client)
    _set_billing_details(client, admin_headers)

    affiliate = client.post("/admin/affiliates", headers=admin_headers, json={
        "name": "Pct Referrer", "commission_type": "PERCENTAGE_RECURRING", "commission_value": 10,
    }).json()

    client.post("/auth/register", json={
        "email": "pctref@example.com", "password": "testpass123",
        "company_name": "Pct Ref Tenant", "country_code": "DE",
        "referral_code": affiliate["referral_code"], "early_service_consent": True,
    })
    tenants = client.get("/admin/tenants", headers=admin_headers).json()
    referred_tenant = next(t for t in tenants if t["company_name"] == "Pct Ref Tenant")

    # net 100 -> 19% VAT -> gross 119 -> 10% commission = 11.90
    _confirm_payment(client, admin_headers, referred_tenant["id"], amount=100)
    after_first = client.get("/admin/affiliates", headers=admin_headers).json()
    affiliate_after_first = next(a for a in after_first if a["id"] == affiliate["id"])
    assert float(affiliate_after_first["balance_owed"]) == 11.90

    _confirm_payment(client, admin_headers, referred_tenant["id"], amount=100)
    after_second = client.get("/admin/affiliates", headers=admin_headers).json()
    affiliate_after_second = next(a for a in after_second if a["id"] == affiliate["id"])
    assert float(affiliate_after_second["balance_owed"]) == 23.80  # earns again on the second payment too

    commissions = client.get(f"/admin/affiliates/{affiliate['id']}/commissions", headers=admin_headers).json()
    assert len(commissions) == 2


def test_disabled_affiliate_earns_no_further_commission(client, register):
    admin_headers, _ = register(email="admin5@example.com")
    _make_superadmin(admin_headers, client)
    _set_billing_details(client, admin_headers)

    affiliate = client.post("/admin/affiliates", headers=admin_headers, json={
        "name": "To Disable", "commission_type": "PERCENTAGE_RECURRING", "commission_value": 10,
    }).json()

    client.post("/auth/register", json={
        "email": "disableref@example.com", "password": "testpass123",
        "company_name": "Disable Ref Tenant", "country_code": "DE",
        "referral_code": affiliate["referral_code"], "early_service_consent": True,
    })
    tenants = client.get("/admin/tenants", headers=admin_headers).json()
    referred_tenant = next(t for t in tenants if t["company_name"] == "Disable Ref Tenant")

    patch_response = client.patch(f"/admin/affiliates/{affiliate['id']}", headers=admin_headers, json={"status": "DISABLED"})
    assert patch_response.status_code == 200
    assert patch_response.json()["status"] == "DISABLED"

    _confirm_payment(client, admin_headers, referred_tenant["id"], amount=100)
    after = client.get("/admin/affiliates", headers=admin_headers).json()
    affiliate_after = next(a for a in after if a["id"] == affiliate["id"])
    assert float(affiliate_after["balance_owed"]) == 0  # disabled - no commission


def test_payout_reduces_balance_but_not_total_earned(client, register):
    admin_headers, _ = register(email="admin6@example.com")
    _make_superadmin(admin_headers, client)
    _set_billing_details(client, admin_headers)

    affiliate = client.post("/admin/affiliates", headers=admin_headers, json={
        "name": "Payout Test", "commission_type": "FLAT_ONE_TIME", "commission_value": 50,
    }).json()

    client.post("/auth/register", json={
        "email": "payoutref@example.com", "password": "testpass123",
        "company_name": "Payout Ref Tenant", "country_code": "DE",
        "referral_code": affiliate["referral_code"], "early_service_consent": True,
    })
    tenants = client.get("/admin/tenants", headers=admin_headers).json()
    referred_tenant = next(t for t in tenants if t["company_name"] == "Payout Ref Tenant")
    _confirm_payment(client, admin_headers, referred_tenant["id"], amount=29)

    payout_response = client.post(f"/admin/affiliates/{affiliate['id']}/payout", headers=admin_headers, json={"amount": 30, "note": "Bank transfer"})
    assert payout_response.status_code == 200
    body = payout_response.json()
    assert float(body["balance_owed"]) == 20  # 50 - 30
    assert float(body["total_earned"]) == 50  # lifetime total untouched


def test_payout_rejects_amount_over_balance(client, register):
    admin_headers, _ = register(email="admin7@example.com")
    _make_superadmin(admin_headers, client)

    affiliate = client.post("/admin/affiliates", headers=admin_headers, json={
        "name": "Overpay Test", "commission_type": "FLAT_ONE_TIME", "commission_value": 10,
    }).json()

    response = client.post(f"/admin/affiliates/{affiliate['id']}/payout", headers=admin_headers, json={"amount": 999})
    assert response.status_code == 400
