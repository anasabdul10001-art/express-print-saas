"""Input errors reach the page as text, never as a list of objects."""


def test_bad_login_email_is_a_sentence_without_the_password(client):
    r = client.post("/auth/login", json={"email": "someone@example.com6", "password": "geheim-123456"})
    assert r.status_code == 422
    assert r.json() == {"detail": "Bitte eine gültige E-Mail-Adresse eingeben."}
    assert "geheim-123456" not in r.text


def test_negative_amount_and_missing_fields_are_named(client, register):
    headers, _ = register()
    r = client.post("/expenses", headers=headers,
                    json={"description": "x", "amount": -5, "expense_date": "2026-11-01"})
    assert r.status_code == 422 and r.json()["detail"].startswith("Betrag: muss ")
    r = client.post("/expenses", headers=headers, json={"expense_date": "kein-datum"})
    detail = r.json()["detail"]
    assert "Beschreibung: fehlt." in detail and "Betrag: fehlt." in detail and "Datum: ist " in detail


def test_unreadable_request_body(client, register):
    headers, _ = register()
    r = client.post("/expenses", headers={**headers, "Content-Type": "application/json"}, content="{kaputt")
    assert r.status_code == 422 and r.json()["detail"] == "Die Anfrage konnte nicht gelesen werden."
