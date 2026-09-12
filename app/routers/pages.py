from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.content.site_pages import PAGE_DEFINITIONS
from app.core.deps import get_db
from app.models.site_page import SitePage
from app.schemas.site_page import SitePageOut

router = APIRouter(prefix="/pages", tags=["public"])


def get_or_create_page(db: Session, slug: str) -> SitePage:
    definition = PAGE_DEFINITIONS.get(slug)
    if not definition:
        raise HTTPException(status_code=404, detail="Seite nicht gefunden")

    page = db.query(SitePage).filter(SitePage.slug == slug).first()
    if not page:
        page = SitePage(slug=slug, title=definition["title"], content=definition["content"])
        db.add(page)
        db.commit()
        db.refresh(page)
    return page


@router.get("/{slug}", response_model=SitePageOut)
def get_page(slug: str, db: Session = Depends(get_db)):
    """
    Deliberately no auth dependency - every static content page (Impressum,
    FAQ, pricing footer links, ...) fetches its text from here for anonymous
    visitors.
    """
    return get_or_create_page(db, slug)
