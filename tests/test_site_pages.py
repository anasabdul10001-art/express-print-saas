"""
Admin-editable public content pages (About/FAQ/Policies/Contact/Withdrawal) - see
app/models/site_page.py and app/routers/plans.py's DEFAULT_SITE_PAGES.
"""


def _make_superadmin(headers, client):
    from sqlalchemy import create_engine, text
    from app.config import settings

    me = client.get("/auth/me", headers=headers).json()
    engine = create_engine(settings.database_url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE users SET is_superadmin = true WHERE id = :id"), {"id": me["id"]})


def test_public_page_returns_default_content_on_first_request(client):
    response = client.get("/pages/about")
    assert response.status_code == 200
    body = response.json()
    assert body["slug"] == "about"
    assert body["title"]
    assert body["content"]


def test_public_page_unknown_slug_returns_404(client):
    response = client.get("/pages/does-not-exist")
    assert response.status_code == 404


def test_faq_page_has_default_content(client):
    response = client.get("/pages/faq")
    assert response.status_code == 200
    body = response.json()
    assert body["slug"] == "faq"
    assert "Frage" in body["content"]


def test_withdrawal_page_has_official_template_content(client):
    response = client.get("/pages/withdrawal")
    assert response.status_code == 200
    body = response.json()
    assert body["slug"] == "withdrawal"
    assert "Widerrufsrecht" in body["content"]
    assert "Muster-Widerrufsformular" in body["content"]


def test_admin_pages_requires_superadmin(client, register):
    headers, _ = register()
    response = client.get("/admin/pages", headers=headers)
    assert response.status_code == 403


def test_admin_pages_lists_all_known_slugs(client, register):
    headers, _ = register()
    _make_superadmin(headers, client)

    response = client.get("/admin/pages", headers=headers)
    assert response.status_code == 200
    slugs = {p["slug"] for p in response.json()}
    assert slugs == {"about", "faq", "policies", "contact", "withdrawal"}


def test_update_page_reflects_on_public_endpoint(client, register):
    headers, _ = register()
    _make_superadmin(headers, client)

    response = client.patch("/admin/pages/contact", headers=headers, json={
        "title": "Kontaktiere uns",
        "content": "E-Mail: hallo@example.com",
    })
    assert response.status_code == 200
    assert response.json()["title"] == "Kontaktiere uns"

    public = client.get("/pages/contact").json()
    assert public["title"] == "Kontaktiere uns"
    assert public["content"] == "E-Mail: hallo@example.com"


def test_partial_update_leaves_other_field_unchanged(client, register):
    headers, _ = register()
    _make_superadmin(headers, client)

    original = client.get("/admin/pages", headers=headers).json()
    original_about = next(p for p in original if p["slug"] == "about")

    response = client.patch("/admin/pages/about", headers=headers, json={"content": "Neuer Inhalt."})
    assert response.status_code == 200
    body = response.json()
    assert body["content"] == "Neuer Inhalt."
    assert body["title"] == original_about["title"]  # untouched


def test_update_unknown_slug_returns_404(client, register):
    headers, _ = register()
    _make_superadmin(headers, client)

    response = client.patch("/admin/pages/does-not-exist", headers=headers, json={"title": "X"})
    assert response.status_code == 404


def test_non_superadmin_cannot_update_pages(client, register):
    headers, _ = register()
    response = client.patch("/admin/pages/about", headers=headers, json={"title": "Hacked"})
    assert response.status_code == 403
