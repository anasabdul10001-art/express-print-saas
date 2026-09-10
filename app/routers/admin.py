import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import get_current_superadmin, get_db
from app.models.plan import Plan, SiteSettings
from app.models.user import User
from app.schemas.admin import PlanCreate, PlanOut, PlanUpdate, SiteSettingsOut

router = APIRouter(prefix="/admin", tags=["admin"])

# Reuses the same Supabase bucket product images already go into (see
# app/routers/uploads.py) rather than requiring a second bucket to be
# created in the Supabase dashboard just for one logo file.
LOGO_BUCKET = "product-images"
ALLOWED_LOGO_TYPES = {"image/jpeg", "image/png", "image/webp", "image/svg+xml"}


@router.get("/plans", response_model=list[PlanOut])
def list_all_plans(
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    """Includes inactive plans too - unlike GET /plans, which is what the public pricing page uses."""
    return db.query(Plan).order_by(Plan.display_order.asc(), Plan.created_at.asc()).all()


@router.post("/plans", response_model=PlanOut, status_code=201)
def create_plan(
    payload: PlanCreate,
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    plan = Plan(**payload.model_dump())
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


@router.patch("/plans/{plan_id}", response_model=PlanOut)
def update_plan(
    plan_id: str,
    payload: PlanUpdate,
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    plan = db.query(Plan).filter(Plan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan nicht gefunden")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(plan, field, value)

    db.commit()
    db.refresh(plan)
    return plan


@router.delete("/plans/{plan_id}", status_code=204)
def delete_plan(
    plan_id: str,
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    plan = db.query(Plan).filter(Plan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan nicht gefunden")
    db.delete(plan)
    db.commit()


def _get_or_create_settings(db: Session) -> SiteSettings:
    settings_row = db.query(SiteSettings).first()
    if not settings_row:
        settings_row = SiteSettings()
        db.add(settings_row)
        db.commit()
        db.refresh(settings_row)
    return settings_row


@router.get("/settings", response_model=SiteSettingsOut)
def get_site_settings(
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    return _get_or_create_settings(db)


@router.post("/logo", response_model=SiteSettingsOut)
async def upload_logo(
    file: UploadFile,
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    if file.content_type not in ALLOWED_LOGO_TYPES:
        raise HTTPException(status_code=400, detail="Nur JPEG, PNG, WEBP oder SVG erlaubt.")

    contents = await file.read()
    if len(contents) > 2 * 1024 * 1024:  # 2 MB - a logo has no reason to be bigger
        raise HTTPException(status_code=400, detail="Datei zu groß (max. 2 MB).")

    extension = file.filename.rsplit(".", 1)[-1] if "." in file.filename else "png"
    # Fixed path (not a random uuid) so re-uploading the logo simply
    # overwrites the previous one instead of accumulating old files.
    path = f"site/logo.{extension}"

    upload_url = f"{settings.supabase_url}/storage/v1/object/{LOGO_BUCKET}/{path}"

    async with httpx.AsyncClient() as client:
        response = await client.post(
            upload_url,
            content=contents,
            headers={
                "Authorization": f"Bearer {settings.supabase_service_key}",
                "apikey": settings.supabase_service_key,
                "Content-Type": file.content_type,
                "x-upsert": "true",  # overwrite if a logo was already uploaded at this path
            },
        )

    if response.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail="Logo-Upload fehlgeschlagen.")

    # Cache-bust: without a changing query string, browsers/CDNs would keep
    # showing the old logo image at the same URL after a re-upload.
    public_url = f"{settings.supabase_url}/storage/v1/object/public/{LOGO_BUCKET}/{path}?v={uuid.uuid4().hex[:8]}"

    settings_row = _get_or_create_settings(db)
    settings_row.logo_url = public_url
    db.commit()
    db.refresh(settings_row)
    return settings_row
