"""Registration, login, and the /auth/me profile-management endpoints."""


def test_register_creates_tenant_and_returns_token(client):
    response = client.post("/auth/register", json={
        "email": "owner@example.com", "password": "testpass123",
        "company_name": "Acme GmbH", "country_code": "DE", "early_service_consent": True,
    })
    assert response.status_code == 201
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]


def test_register_rejects_duplicate_email(client):
    payload = {
        "email": "dupe@example.com", "password": "testpass123", "company_name": "Co A",
        "country_code": "DE", "early_service_consent": True,
    }
    assert client.post("/auth/register", json=payload).status_code == 201
    response = client.post("/auth/register", json=payload)
    assert response.status_code == 400


def test_register_rejects_short_password(client):
    response = client.post("/auth/register", json={
        "email": "shortpw@example.com", "password": "short", "company_name": "Co",
        "country_code": "DE", "early_service_consent": True,
    })
    assert response.status_code == 422


def test_register_requires_early_service_consent_field(client):
    """Missing entirely (not just false) - Pydantic itself rejects it since
    there's no default (see RegisterRequest.early_service_consent)."""
    response = client.post("/auth/register", json={
        "email": "noconsentfield@example.com", "password": "testpass123", "company_name": "Co",
    })
    assert response.status_code == 422


def test_register_rejects_declined_early_service_consent(client):
    """§ 356 Abs. 4 BGB: since ShipSync grants full access immediately, a
    customer must affirmatively accept losing the 14-day withdrawal right -
    declining it (false) blocks the registration rather than silently
    proceeding."""
    response = client.post("/auth/register", json={
        "email": "declinedconsent@example.com", "password": "testpass123", "company_name": "Co",
        "country_code": "DE", "early_service_consent": False,
    })
    assert response.status_code == 400


def test_register_stores_consent_timestamp(client, register):
    from sqlalchemy import create_engine, text
    from app.config import settings

    headers, _ = register(email="consenttimestamp@example.com")
    me = client.get("/auth/me", headers=headers).json()

    engine = create_engine(settings.database_url)
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT early_service_consent_at FROM users WHERE id = :id"), {"id": me["id"]}
        ).first()
    assert row.early_service_consent_at is not None


def test_login_succeeds_with_correct_credentials(client, register):
    _, payload = register(email="loginme@example.com")
    response = client.post("/auth/login", json={"email": "loginme@example.com", "password": payload["password"]})
    assert response.status_code == 200
    assert response.json()["access_token"]


def test_login_rejects_wrong_password(client, register):
    register(email="wrongpw@example.com")
    response = client.post("/auth/login", json={"email": "wrongpw@example.com", "password": "wrongpassword"})
    assert response.status_code == 401


def test_login_rejects_unknown_email(client):
    response = client.post("/auth/login", json={"email": "nobody@example.com", "password": "whatever123"})
    assert response.status_code == 401


def test_superadmin_cannot_use_regular_login(client, register):
    """A superadmin is only ever flipped directly in the DB - simulate that,
    then confirm /auth/login refuses them (must use /auth/admin-login)."""
    headers, payload = register(email="admin@example.com")
    me = client.get("/auth/me", headers=headers).json()

    from sqlalchemy import create_engine, text
    from app.config import settings
    engine = create_engine(settings.database_url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE users SET is_superadmin = true WHERE id = :id"), {"id": me["id"]})

    response = client.post("/auth/login", json={"email": "admin@example.com", "password": payload["password"]})
    assert response.status_code == 403

    admin_response = client.post("/auth/admin-login", json={"email": "admin@example.com", "password": payload["password"]})
    assert admin_response.status_code == 200


def test_login_rate_limited_after_repeated_failures(client, register):
    register(email="ratelimited@example.com")

    for _ in range(5):
        response = client.post("/auth/login", json={"email": "ratelimited@example.com", "password": "wrongpassword"})
        assert response.status_code == 401

    blocked = client.post("/auth/login", json={"email": "ratelimited@example.com", "password": "wrongpassword"})
    assert blocked.status_code == 429


def test_admin_login_rate_limited_after_repeated_failures(client, register):
    headers, payload = register(email="ratelimitedadmin@example.com")
    from sqlalchemy import create_engine, text
    from app.config import settings

    me = client.get("/auth/me", headers=headers).json()
    engine = create_engine(settings.database_url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE users SET is_superadmin = true WHERE id = :id"), {"id": me["id"]})

    for _ in range(5):
        response = client.post("/auth/admin-login", json={"email": "ratelimitedadmin@example.com", "password": "wrongpassword"})
        assert response.status_code == 401

    blocked = client.post("/auth/admin-login", json={"email": "ratelimitedadmin@example.com", "password": "wrongpassword"})
    assert blocked.status_code == 429


def test_me_requires_authentication(client):
    response = client.get("/auth/me")
    assert response.status_code == 401


def test_me_returns_current_user(client, register):
    headers, payload = register(email="whoami@example.com", full_name="Jane Seller")
    response = client.get("/auth/me", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "whoami@example.com"
    assert body["full_name"] == "Jane Seller"
    assert body["is_superadmin"] is False


def test_update_password_requires_correct_current_password(client, register):
    headers, _ = register(email="pwchange@example.com")
    response = client.patch("/auth/me/password", headers=headers, json={
        "current_password": "wrongcurrent", "new_password": "newpassword123",
    })
    assert response.status_code == 401


def test_update_password_then_login_with_new_password(client, register):
    headers, _ = register(email="pwchange2@example.com")
    response = client.patch("/auth/me/password", headers=headers, json={
        "current_password": "testpass123", "new_password": "newpassword123",
    })
    assert response.status_code == 200

    old_login = client.post("/auth/login", json={"email": "pwchange2@example.com", "password": "testpass123"})
    assert old_login.status_code == 401

    new_login = client.post("/auth/login", json={"email": "pwchange2@example.com", "password": "newpassword123"})
    assert new_login.status_code == 200


def test_update_email_rejects_duplicate(client, register):
    register(email="taken@example.com")
    headers, _ = register(email="original@example.com")
    response = client.patch("/auth/me/email", headers=headers, json={
        "email": "taken@example.com", "current_password": "testpass123",
    })
    assert response.status_code == 409
