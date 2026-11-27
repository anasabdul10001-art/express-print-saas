"""Removing the uploaded logo brings back the built-in one."""
from sqlalchemy import text

from app.database import SessionLocal, engine
from app.models.plan import SiteSettings


def _make_superadmin(headers, client):
    me = client.get("/auth/me", headers=headers).json()
    with engine.begin() as conn:
        conn.execute(text("UPDATE users SET is_superadmin = true WHERE id = :id"), {"id": me["id"]})


def _upload_stored():
    with SessionLocal() as db:
        db.add(SiteSettings(logo_url="https://example.test/logo.png", logo_height=72, company_legal_name="Firma"))
        db.commit()


def test_only_the_superadmin_can_remove_the_logo(client, register):
    headers, _ = register()
    _upload_stored()
    assert client.delete("/admin/logo", headers=headers).status_code == 403
    assert client.get("/site-settings").json()["logo_url"] == "https://example.test/logo.png"


def test_removing_the_logo_keeps_the_other_settings(client, register):
    headers, _ = register()
    _make_superadmin(headers, client)
    _upload_stored()
    r = client.delete("/admin/logo", headers=headers)
    assert r.status_code == 200, r.text
    public = client.get("/site-settings").json()
    assert public["logo_url"] is None and public["logo_height"] is None
    assert public["company_legal_name"] == "Firma"
