"""
Self-service affiliate portal: login, forgot/set password, and the
affiliate's own view of their referral stats, commissions, and payouts -
see app/routers/affiliate_portal.py.

Since only the SHA-256 hash of a set-password token is ever stored (the raw
token only ever exists in the emailed link, and no RESEND_API_KEY is
configured in this test environment), tests that need a real token insert
one directly into affiliate_password_tokens - the same trick _make_superadmin
below uses to flip a flag no API lets a test set directly.
"""

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, text

from app.config import settings
from app.core.security import hash_api_key


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


def _confirm_payment(client, admin_headers, tenant_id, amount):
    return client.post(f"/admin/tenants/{tenant_id}/confirm-payment", headers=admin_headers, json={"amount": amount})


def _create_affiliate(client, admin_headers, email, commission_type="PERCENTAGE_RECURRING", commission_value=10):
    response = client.post("/admin/affiliates", headers=admin_headers, json={
        "name": "Portal Test Partner", "email": email,
        "commission_type": commission_type, "commission_value": commission_value,
    })
    assert response.status_code == 201, response.text
    return response.json()


def _inject_password_token(affiliate_id, ttl_hours=1):
    """Simulates the emailed "set your password" link's token."""
    raw_token = secrets.token_urlsafe(32)
    engine = create_engine(settings.database_url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO affiliate_password_tokens (id, affiliate_id, token_hash, expires_at) "
                "VALUES (:id, :affiliate_id, :token_hash, :expires_at)"
            ),
            {
                "id": str(uuid.uuid4()),
                "affiliate_id": affiliate_id,
                "token_hash": hash_api_key(raw_token),
                "expires_at": datetime.now(timezone.utc) + timedelta(hours=ttl_hours),
            },
        )
    return raw_token


def _set_password_and_login(client, email, password="partnerpass123", **create_kwargs):
    admin_headers = create_kwargs.pop("admin_headers")
    affiliate = _create_affiliate(client, admin_headers, email, **create_kwargs)
    raw_token = _inject_password_token(affiliate["id"])
    set_response = client.post("/affiliate/set-password", json={"token": raw_token, "new_password": password})
    assert set_response.status_code == 200, set_response.text

    login_response = client.post("/affiliate/login", json={"email": email, "password": password})
    assert login_response.status_code == 200, login_response.text
    token = login_response.json()["access_token"]
    return affiliate, {"Authorization": f"Bearer {token}"}


def test_affiliate_login_fails_before_password_is_set(client, register):
    admin_headers, _ = register(email="portaladmin1@example.com")
    _make_superadmin(admin_headers, client)
    _create_affiliate(client, admin_headers, "nopassword@example.com")

    response = client.post("/affiliate/login", json={"email": "nopassword@example.com", "password": "whatever123"})
    assert response.status_code == 401


def test_affiliate_set_password_then_login_succeeds(client, register):
    admin_headers, _ = register(email="portaladmin2@example.com")
    _make_superadmin(admin_headers, client)

    affiliate, headers = _set_password_and_login(
        client, "setpw@example.com", admin_headers=admin_headers,
    )
    assert "Authorization" in headers


def test_affiliate_set_password_rejects_invalid_token(client):
    response = client.post("/affiliate/set-password", json={"token": "not-a-real-token", "new_password": "newpass123"})
    assert response.status_code == 400


def test_affiliate_set_password_rejects_expired_token(client, register):
    admin_headers, _ = register(email="portaladmin3@example.com")
    _make_superadmin(admin_headers, client)
    affiliate = _create_affiliate(client, admin_headers, "expiredtoken@example.com")
    raw_token = _inject_password_token(affiliate["id"], ttl_hours=-1)  # already expired

    response = client.post("/affiliate/set-password", json={"token": raw_token, "new_password": "newpass123"})
    assert response.status_code == 400


def test_affiliate_forgot_password_returns_generic_message_regardless(client, register):
    admin_headers, _ = register(email="portaladmin4@example.com")
    _make_superadmin(admin_headers, client)
    _create_affiliate(client, admin_headers, "hasaccount@example.com")

    known = client.post("/affiliate/forgot-password", json={"email": "hasaccount@example.com"})
    unknown = client.post("/affiliate/forgot-password", json={"email": "doesnotexist@example.com"})

    assert known.status_code == 200
    assert unknown.status_code == 200
    assert known.json()["message"] == unknown.json()["message"]


def test_affiliate_me_requires_authentication(client):
    response = client.get("/affiliate/me")
    assert response.status_code == 401


def test_affiliate_me_returns_profile(client, register):
    admin_headers, _ = register(email="portaladmin5@example.com")
    _make_superadmin(admin_headers, client)
    affiliate, headers = _set_password_and_login(client, "meprofile@example.com", admin_headers=admin_headers)

    response = client.get("/affiliate/me", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["referral_code"] == affiliate["referral_code"]
    assert body["status"] == "ACTIVE"
    assert float(body["balance_owed"]) == 0
    assert body["referral_count"] == 0


def test_affiliate_token_cannot_access_user_endpoints(client, register):
    admin_headers, _ = register(email="portaladmin6@example.com")
    _make_superadmin(admin_headers, client)
    _, affiliate_headers = _set_password_and_login(client, "crossaccess1@example.com", admin_headers=admin_headers)

    response = client.get("/auth/me", headers=affiliate_headers)
    assert response.status_code == 401


def test_user_token_cannot_access_affiliate_endpoints(client, register):
    user_headers, _ = register(email="crossaccess2@example.com")

    response = client.get("/affiliate/me", headers=user_headers)
    assert response.status_code == 401


def test_disabled_affiliate_cannot_login(client, register):
    admin_headers, _ = register(email="portaladmin7@example.com")
    _make_superadmin(admin_headers, client)
    affiliate = _create_affiliate(client, admin_headers, "disabledpartner@example.com")
    raw_token = _inject_password_token(affiliate["id"])
    client.post("/affiliate/set-password", json={"token": raw_token, "new_password": "partnerpass123"})

    client.patch(f"/admin/affiliates/{affiliate['id']}", headers=admin_headers, json={"status": "DISABLED"})

    response = client.post("/affiliate/login", json={"email": "disabledpartner@example.com", "password": "partnerpass123"})
    assert response.status_code == 403


def test_affiliate_sees_only_own_commissions_and_payouts(client, register):
    admin_headers, _ = register(email="portaladmin8@example.com")
    _make_superadmin(admin_headers, client)
    _set_billing_details(client, admin_headers)

    affiliate_a, headers_a = _set_password_and_login(
        client, "partnera@example.com", admin_headers=admin_headers,
        commission_type="FLAT_ONE_TIME", commission_value=20,
    )
    _affiliate_b, headers_b = _set_password_and_login(
        client, "partnerb@example.com", admin_headers=admin_headers,
        commission_type="FLAT_ONE_TIME", commission_value=20,
    )

    referred_response = client.post("/auth/register", json={
        "email": "referredbya2@example.com", "password": "testpass123", "company_name": "Referred by A",
        "early_service_consent": True, "referral_code": affiliate_a["referral_code"],
    })
    assert referred_response.status_code == 201

    tenants = client.get("/admin/tenants", headers=admin_headers).json()
    referred_tenant = next(t for t in tenants if t["company_name"] == "Referred by A")
    _confirm_payment(client, admin_headers, referred_tenant["id"], amount=29)

    commissions_a = client.get("/affiliate/commissions", headers=headers_a).json()
    commissions_b = client.get("/affiliate/commissions", headers=headers_b).json()
    assert len(commissions_a) == 1
    assert float(commissions_a[0]["amount"]) == 20
    assert len(commissions_b) == 0

    payout_response = client.post(
        f"/admin/affiliates/{affiliate_a['id']}/payout", headers=admin_headers, json={"amount": 20, "note": "Bank transfer"},
    )
    assert payout_response.status_code == 200

    payouts_a = client.get("/affiliate/payouts", headers=headers_a).json()
    payouts_b = client.get("/affiliate/payouts", headers=headers_b).json()
    assert len(payouts_a) == 1
    assert float(payouts_a[0]["amount"]) == 20
    assert len(payouts_b) == 0

    me_a = client.get("/affiliate/me", headers=headers_a).json()
    assert float(me_a["total_earned"]) == 20
    assert float(me_a["balance_owed"]) == 0  # paid out in full
    assert me_a["referral_count"] == 1


def test_affiliate_login_rate_limited_after_repeated_failures(client, register):
    admin_headers, _ = register(email="portaladmin9@example.com")
    _make_superadmin(admin_headers, client)
    affiliate = _create_affiliate(client, admin_headers, "ratelimitedpartner@example.com")
    raw_token = _inject_password_token(affiliate["id"])
    client.post("/affiliate/set-password", json={"token": raw_token, "new_password": "partnerpass123"})

    # 5 failed attempts (not the one successful login above, which would
    # otherwise eat into the same 5/minute budget) - the 6th is blocked.
    for _ in range(5):
        response = client.post("/affiliate/login", json={"email": "ratelimitedpartner@example.com", "password": "wrongpassword"})
        assert response.status_code == 401

    blocked = client.post("/affiliate/login", json={"email": "ratelimitedpartner@example.com", "password": "wrongpassword"})
    assert blocked.status_code == 429
