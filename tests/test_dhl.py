"""DHL account connect/status/disconnect - credential storage only (see
app/routers/dhl.py's own docstring for why there's no live API call here)."""

DHL_CONNECT_PAYLOAD = {
    "billing_number": "33333333330101",
    "api_username": "user-valid",
    "api_password": "SandboxPasswort2023!",
    "sender_name": "Test Sender GmbH",
    "sender_street": "Teststraße 1",
    "sender_zip": "10115",
    "sender_city": "Berlin",
    "sender_country": "de",
}


def test_status_is_null_before_connecting(client, register):
    headers, _ = register()
    response = client.get("/dhl/status", headers=headers)
    assert response.status_code == 200
    assert response.json() is None


def test_connect_then_status_reflects_connected_account(client, register):
    headers, _ = register()
    connect_response = client.post("/dhl/connect", headers=headers, json=DHL_CONNECT_PAYLOAD)
    assert connect_response.status_code == 200
    body = connect_response.json()
    assert body["billing_number"] == "33333333330101"
    assert body["sender_country"] == "DE"  # uppercased
    assert body["status"] == "CONNECTED"
    assert "api_username" not in body and "api_password" not in body

    status_response = client.get("/dhl/status", headers=headers)
    assert status_response.json()["billing_number"] == "33333333330101"


def test_credentials_are_encrypted_at_rest(client, register):
    headers, _ = register()
    client.post("/dhl/connect", headers=headers, json=DHL_CONNECT_PAYLOAD)

    from sqlalchemy import create_engine, text
    from app.config import settings
    engine = create_engine(settings.database_url)
    with engine.begin() as conn:
        row = conn.execute(text("SELECT api_username_encrypted FROM dhl_accounts")).first()
    assert row is not None
    assert DHL_CONNECT_PAYLOAD["api_username"] not in row[0]  # never stored in plaintext


def test_disconnect_without_account_returns_404(client, register):
    headers, _ = register()
    response = client.post("/dhl/disconnect", headers=headers)
    assert response.status_code == 404


def test_disconnect_clears_credentials_and_status(client, register):
    headers, _ = register()
    client.post("/dhl/connect", headers=headers, json=DHL_CONNECT_PAYLOAD)

    response = client.post("/dhl/disconnect", headers=headers)
    assert response.status_code == 200

    status_response = client.get("/dhl/status", headers=headers)
    assert status_response.json() is None  # disconnected accounts don't count as "connected"


def test_dhl_accounts_are_isolated_per_tenant(client, register):
    headers_a, _ = register()
    headers_b, _ = register()

    client.post("/dhl/connect", headers=headers_a, json=DHL_CONNECT_PAYLOAD)

    assert client.get("/dhl/status", headers=headers_b).json() is None
    assert client.get("/dhl/status", headers=headers_a).json() is not None
