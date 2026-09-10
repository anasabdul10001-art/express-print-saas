from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models.plan import Plan, SiteSettings
from app.schemas.admin import PlanOut, SiteSettingsOut

router = APIRouter(tags=["public"])


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


@router.get("/site-settings", response_model=SiteSettingsOut)
def get_public_site_settings(db: Session = Depends(get_db)):
    """Public logo URL - used by every page to render the current brand logo."""
    settings_row = db.query(SiteSettings).first()
    if not settings_row:
        return SiteSettingsOut(logo_url=None)
    return settings_row
