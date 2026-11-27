"""Closed sign-up, the legal pages and the corrections to the stored public pages."""
import json
import os
import pathlib

import pytest

from app.config import settings
from app.database import SessionLocal
from app.models.site_page import SitePage
from app.models.user import User
from app.routers.plans import DEFAULT_SITE_PAGES
from app.services import legal_texts, site_page_fixes

OLD_IMPRESSUM = """<h2>Angaben gemäß § 5 TMG</h2>
<p>[PLATZHALTER: Vor- und Nachname bzw. Firmenname]</p>
<p>E-Mail: <a href="mailto:me@example.com">me@example.com</a></p>
<p>Die Europäische Kommission stellt eine Plattform zur Online-Streitbeilegung (OS) bereit:
<a href="https://ec.europa.eu/consumers/odr/">https://ec.europa.eu/consumers/odr/</a>.</p>
"""
CUSTOMERS = """<h1>Erfolgsgeschichten</h1>
<blockquote>"Express Print hat uns pro Tag mehrere Stunden manuellen Etikettendruck
erspart." - [PLATZHALTER: Kundenname, Unternehmen]</blockquote>"""
FORBIDDEN = ("[PLATZHALTER", "consumers/odr", "TMG", "RStV", "Express Print", "Express-Print", "Stunden manuellen")


@pytest.fixture
def operator(monkeypatch):
    for name, value in (("operator_name", "Max <Muster>"), ("operator_street", "Weg 1"),
                        ("operator_city", "01234 Ort"), ("operator_email", "hallo@muster.de"),
                        ("operator_phone", ""), ("operator_vat_id", "")):
        monkeypatch.setattr(settings, name, value)


def _payload(email="neu@example.com"):
    return {"email": email, "password": "testpass123", "company_name": "Neu", "country_code": "DE",
            "business_confirmed": True, "terms_accepted": True}


def _page(client, slug):
    return client.get(f"/pages/{slug}").json()["content"]


def test_the_server_refuses_sign_up_while_it_is_closed(client, monkeypatch):
    monkeypatch.setattr(settings, "registration_open", False)
    assert client.get("/auth/registration-status").json() == {"open": False}
    r = client.post("/auth/register", json=_payload())
    assert r.status_code == 403 and "geschlossen" in r.json()["detail"]
    with SessionLocal() as db:
        assert db.query(User).count() == 0


def test_existing_accounts_still_log_in_while_closed(client, register, monkeypatch):
    _, payload = register()
    monkeypatch.setattr(settings, "registration_open", False)
    r = client.post("/auth/login", json={"email": payload["email"], "password": payload["password"]})
    assert r.status_code == 200, r.text


def test_sign_up_is_closed_unless_the_server_opens_it():
    assert type(settings).model_fields["registration_open"].default is False


@pytest.mark.parametrize("slug", list(DEFAULT_SITE_PAGES))
def test_no_default_page_carries_the_old_mistakes(client, slug, operator):
    content = _page(client, slug)
    assert site_page_fixes.corrected(slug, content) == content
    for word in ("consumers/odr", "TMG", "RStV", "Express Print", "Stunden manuellen", "[PLATZHALTER", "{{"):
        assert word not in content


def test_the_legal_pages_are_complete_once_the_operator_is_set(client, operator):
    for slug in ("impressum", "contact", "policies", "withdrawal", "cookies", "status"):
        content = _page(client, slug)
        assert "{{" not in content and "[PLATZHALTER" not in content and "[wird noch" not in content, slug
    impressum = _page(client, "impressum")
    assert "Max &lt;Muster&gt;" in impressum and "Weg 1" in impressum and "§ 5 DDG" in impressum
    assert "Telefon" not in impressum and "Umsatzsteuer-Identifikationsnummer" not in impressum
    assert "ausschließlich Unternehmern" in _page(client, "policies")
    assert "Verbraucher können kein Konto anlegen" in _page(client, "withdrawal")


def test_optional_lines_appear_when_set_and_missing_details_are_visible(client, operator, monkeypatch):
    monkeypatch.setattr(settings, "operator_phone", "012 345")
    monkeypatch.setattr(settings, "operator_vat_id", "DE000")
    content = _page(client, "impressum")
    assert "Telefon: 012 345" in content and "DE000" in content
    monkeypatch.setattr(settings, "operator_name", "")
    assert "[wird noch ergänzt]" in _page(client, "impressum")
    assert client.get("/legal/operator").json()["name"] == ""


def test_prices_follow_the_small_business_setting(monkeypatch):
    monkeypatch.setattr(settings, "operator_small_business", True)
    assert "§ 19 UStG" in legal_texts.policies()
    monkeypatch.setattr(settings, "operator_small_business", False)
    assert "zuzüglich der gesetzlichen Umsatzsteuer" in legal_texts.policies()


def test_stored_drafts_are_replaced_once_and_keep_the_published_email(client):
    with SessionLocal() as db:
        db.add(SitePage(slug="impressum", title="Impressum", content=OLD_IMPRESSUM))
        db.add(SitePage(slug="customers", title="Kunden", content=CUSTOMERS))
        db.add(SitePage(slug="faq", title="FAQ", content='Was ist Express Print? Siehe <a href="policies.html">AGB</a> und im <a href="withdrawal.html">Widerrufsrecht</a>.'))
        db.add(SitePage(slug="blog", title="Blog", content="Unverändert"))
        db.commit()
        assert site_page_fixes.apply(db) == 3
        assert site_page_fixes.apply(db) == 0
    impressum = _page(client, "impressum")
    assert 'mailto:me@example.com' in impressum and not any(w in impressum for w in FORBIDDEN)
    assert client.get("/legal/operator").json()["email"] == "me@example.com"
    assert _page(client, "faq") == 'Was ist NettoDesk? Siehe <a href="policies.html">AGB</a>.'
    assert _page(client, "blog") == "Unverändert"


def test_stored_pages_get_the_new_product_name_once(client):
    with SessionLocal() as db:
        db.add(SitePage(slug="about", title="Über uns", content="NettoDesk hilft. Der NettoDesk-Print-Agent druckt."))
        db.commit()
        assert site_page_fixes.apply(db) == 1
        assert site_page_fixes.apply(db) == 0
    assert _page(client, "about") == "NettoDesk hilft. Der NettoDesk-Print-Agent druckt."


def test_a_page_the_admin_completed_is_left_alone():
    own = "<h1>Impressum</h1><p>Eigener, fertiger Text.</p>"
    for slug in ("impressum", "policies", "contact", "withdrawal", "customers"):
        assert site_page_fixes.corrected(slug, own) == own


@pytest.mark.parametrize("path, gone", [
    ("frontend/index.html", ["Team-Mitglieder"]),
    ("frontend/translations.js", ["Team-Mitglieder", "team members", "zzgl. MwSt", "register.consentLabel"]),
    ("frontend/pricing.html", ["expressprint.example", "zzgl. MwSt", "pricing.limitUsers"]),
    ("frontend/trial-expired.html", ["expressprint.example"]),
    ("frontend/privacy.html", ["PLATZHALTER", "Express-Print", "Wir speichern aktuell keine"]),
    ("frontend/register.html", ["early_service_consent"]),
])
def test_removed_claims_and_wrong_texts(path, gone):
    text = pathlib.Path(path).read_text(encoding="utf-8")
    assert not [word for word in gone if word in text]


def test_the_corrections_fix_the_pages_that_are_live_today():
    """Against snapshots of the live pages, if given (skipped in CI)."""
    folder = pathlib.Path(os.environ.get("LIVE_PAGES_DIR", "no-such-folder"))
    if not folder.is_dir():
        pytest.skip("no snapshots")
    files = list(folder.glob("*.json"))
    assert files
    for file in files:
        page = json.loads(file.read_text(encoding="utf-8"))
        fixed = site_page_fixes.corrected(page["slug"], page["content"])
        for word in FORBIDDEN:
            assert word not in fixed, (page["slug"], word)
