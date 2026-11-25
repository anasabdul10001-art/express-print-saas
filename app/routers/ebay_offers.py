"""Offers to interested buyers: list what is eligible, send one confirmed offer."""

import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.ebay_account import EbayAccount
from app.models.tenant import Tenant
from app.models.user import User
from app.services import ebay_offers_service as offers

router = APIRouter(prefix="/ebay/offers", tags=["ebay-offers"])


class SendIn(BaseModel):
    account_id: uuid.UUID
    listing_id: str = Field(..., pattern=r"^\d{9,15}$")
    discount_percent: int = Field(..., ge=offers.MIN_DISCOUNT, le=offers.MAX_DISCOUNT)
    message: str = Field("", max_length=2000)
    # Both must be set deliberately by the seller.
    confirm: bool = False
    accept_loss: bool = False


def _connected(db: Session, tenant_id):
    return db.query(EbayAccount).filter(EbayAccount.tenant_id == tenant_id, EbayAccount.status == "CONNECTED")


@router.get("/eligible")
def eligible(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    accounts = _connected(db, current_user.tenant_id).order_by(EbayAccount.connected_at).all()
    if not accounts:
        raise HTTPException(status_code=400, detail="Kein verbundenes eBay-Konto gefunden.")
    vat_mode = db.get(Tenant, current_user.tenant_id).vat_mode
    items, errors = [], []
    for account in accounts:
        name = account.ebay_user_id or "eBay"
        try:
            listing_ids = offers.eligible_listing_ids(account, db)
        except (ValueError, httpx.HTTPError) as exc:
            errors.append(f"{name}: {exc}")
            continue
        for listing_id in listing_ids:
            check = offers.price_check(offers.product_for_listing(db, current_user.tenant_id, listing_id), vat_mode)
            items.append({
                "account_id": str(account.id), "account": name, "listing_id": listing_id, "title": check["title"],
                "price": None if check["price"] is None else str(check["price"]),
                "break_even": None if check["break_even"] is None else str(check["break_even"]),
            })
    if errors and len(errors) == len(accounts):
        raise HTTPException(status_code=502, detail="; ".join(errors))
    return {"items": items, "errors": errors,
            "min_discount": offers.MIN_DISCOUNT, "max_discount": offers.MAX_DISCOUNT}


@router.post("/send")
def send(payload: SendIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Das Angebot muss ausdrücklich bestätigt werden.")
    account = _connected(db, current_user.tenant_id).filter(EbayAccount.id == payload.account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="eBay-Konto nicht gefunden.")

    vat_mode = db.get(Tenant, current_user.tenant_id).vat_mode
    check = offers.price_check(offers.product_for_listing(db, current_user.tenant_id, payload.listing_id), vat_mode)
    if check["price"] is not None and check["break_even"] is not None and not payload.accept_loss:
        new_price = offers.discounted(check["price"], payload.discount_percent)
        if new_price < check["break_even"]:
            raise HTTPException(status_code=409, detail=(
                f"Mit {payload.discount_percent} % Rabatt (ca. {new_price} €) liegt der Preis unter der "
                f"geschätzten Gewinnschwelle von {check['break_even']} €."))
    try:
        count = offers.send_offer(account, db, payload.listing_id, payload.discount_percent, payload.message.strip())
    except offers.SendUncertain:
        raise HTTPException(status_code=502, detail="Keine Antwort von eBay. Bitte zuerst bei eBay prüfen, "
                            "ob das Angebot gesendet wurde, bevor du es erneut versuchst.")
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"sent_to": count}
