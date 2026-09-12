from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.core.crypto import encrypt_token
from app.core.deps import get_current_user, get_db
from app.models.dhl_account import DhlAccount
from app.models.user import User
from app.schemas.dhl import DhlAccountConnect, DhlAccountOut

router = APIRouter(prefix="/dhl", tags=["dhl"])


@router.get("/status", response_model=DhlAccountOut | None)
def get_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Only returns the account if it's actually CONNECTED - same reasoning as
    GET /ebay/status: a DISCONNECTED row still exists after disconnect()
    (credentials cleared, not the row itself).
    """
    return (
        db.query(DhlAccount)
        .filter(DhlAccount.tenant_id == current_user.tenant_id, DhlAccount.status == "CONNECTED")
        .first()
    )


@router.post("/connect", response_model=DhlAccountOut)
def connect_dhl(
    payload: DhlAccountConnect,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Saves this tenant's own DHL Business Customer credentials - there's no
    OAuth consent screen for this API (unlike eBay), so the tenant types
    their DHL login in directly and it's encrypted at rest exactly like
    eBay's tokens are.

    Deliberately does NOT make a live call to DHL to verify these
    credentials yet - the exact request/response shape of the Parcel DE
    Shipping API needs confirming against a real sandbox call first, once
    a real DHL developer-portal API key is available (label purchase itself
    will live in a future app/services/dhl_service.py, wired into
    print_jobs.py in place of today's placeholder label URL). Wrong
    credentials will surface clearly the first time a label is actually
    purchased, rather than a "connection test" here giving false confidence
    about an unverified endpoint.
    """
    account = db.query(DhlAccount).filter(DhlAccount.tenant_id == current_user.tenant_id).first()
    if not account:
        account = DhlAccount(tenant_id=current_user.tenant_id)
        db.add(account)

    account.billing_number = payload.billing_number
    account.api_username_encrypted = encrypt_token(payload.api_username)
    account.api_password_encrypted = encrypt_token(payload.api_password)
    account.sender_name = payload.sender_name
    account.sender_street = payload.sender_street
    account.sender_zip = payload.sender_zip
    account.sender_city = payload.sender_city
    account.sender_country = payload.sender_country.upper()
    account.status = "CONNECTED"
    account.environment = settings.dhl_environment
    account.connected_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(account)
    return account


@router.post("/disconnect")
def disconnect_dhl(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    account = db.query(DhlAccount).filter(DhlAccount.tenant_id == current_user.tenant_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Kein verbundenes DHL-Konto gefunden.")

    account.api_username_encrypted = None
    account.api_password_encrypted = None
    account.status = "DISCONNECTED"
    db.commit()

    return {"status": "disconnected"}
