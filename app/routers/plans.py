from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

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
            "ShipSync hilft eBay-Verkäufern, Bestellungen, Produkte und den Versand an einem Ort zu verwalten.\n\n"
            "[PLATZHALTER: Erzähle hier, wer ihr seid und warum ihr ShipSync gebaut habt.]"
        ),
    },
    "policies": {
        "title": "Richtlinien & Nutzungsbedingungen",
        "content": (
            "Diese Seite beschreibt, wie ShipSync genutzt werden darf.\n\n"
            "[PLATZHALTER: Nutzungsbedingungen, Kündigungsfristen, erlaubte/verbotene Nutzung usw. "
            "Vor Veröffentlichung juristisch prüfen lassen.]"
        ),
    },
    "contact": {
        "title": "Kontakt",
        "content": (
            "Wir helfen dir gerne weiter.\n\n"
            "E-Mail: [PLATZHALTER: Kontakt-E-Mail]\n"
            "Telefon: [PLATZHALTER: Telefonnummer]"
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
    return _get_or_create_page(db, slug)


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
