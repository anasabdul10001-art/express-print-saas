from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.services import legal_texts, operator_service, site_page_fixes
from app.core.deps import get_db
from app.models.payment_method import PaymentMethod
from app.models.plan import Plan, SiteSettings
from app.models.site_page import SitePage
from app.schemas.admin import PaymentMethodOut, PlanOut, SiteSettingsOut
from app.schemas.site_page import SitePageOut

router = APIRouter(tags=["public"])

# The fixed, known set of admin-editable content pages (see app/models/site_page.py's
# docstring for why this isn't an open-ended CMS). Placeholder copy, meant to
# be rewritten from the Super Admin panel before real customers see it.
DEFAULT_SITE_PAGES = {
    "about": {
        "title": "Über uns",
        "content": (
            "ShipSync hilft eBay-Verkäufern, Bestellungen, Produkte und den Versand an einem Ort zu verwalten."
        ),
    },
    "policies": {
        "title": "Allgemeine Geschäftsbedingungen (AGB)",
        "content": legal_texts.policies(),
    },
    "withdrawal": {
        "title": "Widerrufsrecht",
        "content": legal_texts.WITHDRAWAL,
    },
    "contact": {
        "title": "Kontakt",
        "content": legal_texts.CONTACT,
    },
    "faq": {
        "title": "Häufige Fragen",
        "content": (
            "Frage: Wie verbinde ich mein eBay-Konto?\n"
            "Antwort: Gehe im Dashboard auf \"Mit eBay verbinden\" und folge den Anweisungen.\n\n"
            "Frage: Wie drucke ich ein Versandetikett?\n"
            "Antwort: Öffne eine Bestellung in \"Bestellungen\" und klicke auf \"Etikett drucken\".\n\n"
            "Weitere Fragen beantworten wir gern über die Kontaktseite (/contact.html)."
        ),
    },
    "impressum": {
        "title": "Impressum",
        "content": legal_texts.IMPRESSUM,
    },
    "cookies": {
        "title": "Cookie-Hinweis",
        "content": legal_texts.COOKIES,
    },
    "careers": {
        "title": "Karriere",
        "content": (
            "ShipSync wächst - aktuell haben wir keine offenen Stellen ausgeschrieben, freuen "
            "uns aber jederzeit über Initiativbewerbungen.\n\n"
            "Warum bei uns arbeiten?\n\n"
            "Direkter Einfluss auf ein Produkt, das echten eBay-Verkäufern täglich Zeit spart. "
            "Kurze Entscheidungswege, keine Bürokratie. Remote-freundlich.\n\n"
            "Interesse? Schreib uns an {{operator_email}} - gerne mit ein paar Zeilen "
            "zu dir und was dich reizt."
        ),
    },
    "help": {
        "title": "Hilfe-Center",
        "content": (
            "Hier findest du die schnellsten Wege zu Antworten:\n\n"
            "Häufige Fragen (/faq.html) - Antworten auf die häufigsten Fragen.\n\n"
            "Systemstatus (/status.html) - aktueller Betriebsstatus von ShipSync.\n\n"
            "Änderungsprotokoll (/changelog.html) - was sich zuletzt geändert hat.\n\n"
            "Direkter E-Mail-Support: {{operator_email}} - für alles, was hier nicht "
            "beantwortet wird."
        ),
    },
    "status": {
        "title": "Systemstatus",
        "content": legal_texts.STATUS,
    },
    "changelog": {
        "title": "Änderungsprotokoll",
        "content": (
            "Bald verfügbar: weitere Marktplatz-Integrationen, erweiterte "
            "Marktrecherche-Funktionen."
        ),
    },
    "integrations": {
        "title": "Integrationen",
        "content": (
            "ShipSync verbindet sich direkt mit den Werkzeugen, die du als eBay-Verkäufer "
            "bereits nutzt.\n\n"
            "Verfügbar: eBay (automatische Synchronisierung deiner Bestellungen über die "
            "offizielle eBay-API), DHL (Versandetiketten direkt aus ShipSync), lokale "
            "Etikettendrucker über den ShipSync-Print-Agent.\n\n"
            "Geplant: weitere Marktplatz- und Buchhaltungs-Integrationen. Wünsche? Schreib uns "
            "an {{operator_email}}."
        ),
    },
    "customers": {
        "title": "Erfolgsgeschichten",
        "content": site_page_fixes.CUSTOMERS_PAGE,
    },
    "partners": {
        "title": "Partnerprogramm",
        "content": (
            "Du berätst eBay-Verkäufer oder betreibst eine Agentur im E-Commerce-Umfeld? Als "
            "ShipSync-Partner empfiehlst du unseren Dienst weiter und profitierst davon.\n\n"
            "Wie es funktioniert: Du empfiehlst ShipSync an deine Kunden oder dein Netzwerk, "
            "wir stimmen die Konditionen individuell mit dir ab.\n\n"
            "Interesse? Melde dich unter {{operator_email}} - wir melden uns zeitnah "
            "zurück."
        ),
    },
    "blog": {
        "title": "Blog",
        "content": (
            "Hier werden künftig Tipps rund um eBay-Verkauf, Versand und Lagerverwaltung sowie "
            "Produkt-Neuigkeiten geteilt."
        ),
    },
}


def _get_or_create_page(db: Session, slug: str) -> SitePage:
    if slug not in DEFAULT_SITE_PAGES:
        raise HTTPException(status_code=404, detail="Seite nicht gefunden")

    page = db.query(SitePage).filter(SitePage.slug == slug).first()
    if not page:
        defaults = DEFAULT_SITE_PAGES[slug]
        page = SitePage(slug=slug, title=defaults["title"], content=defaults["content"])
        db.add(page)
        db.commit()
        db.refresh(page)
    return page


@router.get("/pages/{slug}", response_model=SitePageOut)
def get_site_page(slug: str, db: Session = Depends(get_db)):
    """Deliberately no auth dependency - these are public marketing pages."""
    page = SitePageOut.model_validate(_get_or_create_page(db, slug))
    return page.model_copy(update={"content": operator_service.render(page.content, operator_service.details(db))})


@router.get("/legal/operator")
def legal_operator(db: Session = Depends(get_db)):
    """The operator's public details, for the static legal pages."""
    return operator_service.details(db)


@router.get("/plans", response_model=list[PlanOut])
def list_active_plans(db: Session = Depends(get_db)):
    """
    Deliberately no auth dependency - a public pricing page needs to show
    this to visitors who haven't registered yet.
    """
    return (
        db.query(Plan)
        .filter(Plan.is_active == True)  # noqa: E712 - SQLAlchemy needs `== True`, not `is True`
        .order_by(Plan.display_order.asc(), Plan.created_at.asc())
        .all()
    )


@router.get("/payment-methods", response_model=list[PaymentMethodOut])
def list_active_payment_methods(db: Session = Depends(get_db)):
    """Deliberately no auth dependency - shown on the public pricing page."""
    return (
        db.query(PaymentMethod)
        .filter(PaymentMethod.is_active == True)  # noqa: E712
        .order_by(PaymentMethod.display_order.asc(), PaymentMethod.created_at.asc())
        .all()
    )


@router.get("/site-settings", response_model=SiteSettingsOut)
def get_public_site_settings(db: Session = Depends(get_db)):
    """Public logo URL - used by every page to render the current brand logo."""
    settings_row = db.query(SiteSettings).first()
    if not settings_row:
        return SiteSettingsOut(
            logo_url=None, logo_height=None, company_legal_name=None,
            company_address=None, company_tax_id=None, company_email=None,
        )
    return settings_row
