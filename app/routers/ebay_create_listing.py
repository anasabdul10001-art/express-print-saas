"""New eBay listings: business policies, a dry-run check, and the confirmed publish."""

import uuid
from datetime import datetime
from decimal import Decimal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import AwareDatetime, BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.ebay_account import EbayAccount
from app.models.product import Product
from app.models.user import User
from app.services import ebay_create_listing_service as service

router = APIRouter(prefix="/ebay/new-listing", tags=["ebay-new-listing"])


class DraftIn(BaseModel):
    account_id: uuid.UUID
    # Made by the page once per draft; eBay refuses the same one twice.
    draft_id: uuid.UUID
    title: str = Field(..., min_length=3, max_length=80)
    description: str = Field(..., min_length=10, max_length=20000)
    category_id: str = Field(..., pattern=r"^\d{1,10}$")
    price: Decimal = Field(..., ge=1, le=100000, decimal_places=2)
    quantity: int = Field(..., ge=1, le=10000)
    condition_id: int = Field(1000, ge=1000, le=7000)
    location: str = Field(..., min_length=2, max_length=45)
    postal_code: str = Field(..., pattern=r"^\d{5}$")
    picture_urls: list[str] = Field(..., min_length=1, max_length=12)
    shipping_policy_id: str = Field(..., pattern=r"^\d+$")
    return_policy_id: str = Field(..., pattern=r"^\d+$")
    payment_policy_id: str = Field(..., pattern=r"^\d+$")
    sku: str = Field("", max_length=50)
    specifics: dict[str, str] = Field(default_factory=dict, max_length=30)
    schedule_at: AwareDatetime | None = None
    confirm: bool = False

    @field_validator("picture_urls")
    @classmethod
    def https_only(cls, urls):
        if any(not u.startswith("https://") or len(u) > 500 for u in urls):
            raise ValueError("Bild-Adressen müssen mit https:// beginnen.")
        return urls

    @field_validator("title", "description", "location", "sku")
    @classmethod
    def no_control_characters(cls, value):
        if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
            raise ValueError("Der Text enthält unzulässige Steuerzeichen.")
        return value.strip()


def _account(db: Session, tenant_id, account_id) -> EbayAccount:
    account = db.query(EbayAccount).filter(
        EbayAccount.id == account_id, EbayAccount.tenant_id == tenant_id, EbayAccount.status == "CONNECTED").first()
    if not account:
        raise HTTPException(status_code=404, detail="eBay-Konto nicht gefunden.")
    return account


def _draft(payload: DraftIn) -> dict:
    if payload.schedule_at and not service.is_future(payload.schedule_at):
        raise HTTPException(status_code=400, detail="Der geplante Zeitpunkt muss in der Zukunft liegen.")
    draft = payload.model_dump(exclude={"account_id", "confirm"})
    draft["draft_id"] = str(payload.draft_id)
    draft["specifics"] = {k.strip(): v.strip() for k, v in payload.specifics.items() if k.strip() and v.strip()}
    return draft


@router.get("/policies")
def policies(account_id: uuid.UUID, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    account = _account(db, current_user.tenant_id, account_id)
    try:
        return service.business_policies(account, db)
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/verify")
def verify(payload: DraftIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    account = _account(db, current_user.tenant_id, payload.account_id)
    try:
        result = service.verify(account, db, _draft(payload))
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    result.pop("item_id", None)  # eBay returns a placeholder here, nothing was listed
    return result


@router.post("/publish")
def publish(payload: DraftIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Das Veröffentlichen muss ausdrücklich bestätigt werden.")
    account = _account(db, current_user.tenant_id, payload.account_id)
    draft = _draft(payload)
    try:
        result = service.publish(account, db, draft)
    except service.PublishUncertain:
        raise HTTPException(status_code=502, detail="Keine eindeutige Antwort von eBay. Bitte zuerst im Verkäufer-Cockpit "
                            "prüfen, ob das Angebot angelegt wurde, bevor du es erneut versuchst.")
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if result["ok"] and result["item_id"]:
        sku = draft["sku"] or result["item_id"]
        if not db.query(Product).filter(Product.tenant_id == current_user.tenant_id, Product.sku == sku).first():
            db.add(Product(tenant_id=current_user.tenant_id, sku=sku, title=draft["title"],
                           selling_price=payload.price, stock_quantity=payload.quantity,
                           image_url=draft["picture_urls"][0]))
            db.commit()
    return result
