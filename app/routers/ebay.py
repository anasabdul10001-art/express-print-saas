from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import get_current_user, get_db
from app.models.ebay_account import EbayAccount
from app.models.user import User
from app.schemas.ebay import AuthorizeUrlOut, EbayAccountOut
from app.services import ebay_oauth_service, ebay_order_service

router = APIRouter(prefix="/ebay", tags=["ebay"])


@router.get("/authorize-url", response_model=AuthorizeUrlOut)
def get_authorize_url(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Returns the eBay consent-screen URL the frontend should redirect the
    browser to when the user clicks "Connect eBay"."""
    url = ebay_oauth_service.build_authorize_url(current_user.tenant_id, db)
    return AuthorizeUrlOut(authorize_url=url)


@router.get("/callback")
def ebay_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: Session = Depends(get_db),
):
    """
    eBay redirects the user's browser here after they approve/deny access.
    Deliberately has NO auth dependency - the browser isn't sending our JWT,
    eBay is sending a signed redirect. Tenant identity is recovered from the
    `state` row created in build_authorize_url(), not from any session.
    """
    settings_page = f"{settings.frontend_url}/app/ebay.html"

    if error:
        return RedirectResponse(f"{settings_page}?ebay_error={error}")
    if not code or not state:
        return RedirectResponse(f"{settings_page}?ebay_error=missing_params")

    try:
        ebay_oauth_service.handle_callback(code, state, db)
    except ValueError as exc:
        return RedirectResponse(f"{settings_page}?ebay_error={exc}")

    return RedirectResponse(f"{settings_page}?ebay_connected=1")


@router.get("/status", response_model=EbayAccountOut | None)
def get_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Only returns the account if it's actually CONNECTED. A DISCONNECTED row
    still exists after disconnect() (tokens nulled, not the row itself), so
    without this filter the frontend would keep showing "connected" forever.
    """
    return (
        db.query(EbayAccount)
        .filter(EbayAccount.tenant_id == current_user.tenant_id, EbayAccount.status == "CONNECTED")
        .first()
    )


@router.post("/disconnect")
def disconnect_ebay(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    account = db.query(EbayAccount).filter(EbayAccount.tenant_id == current_user.tenant_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="No eBay account connected for this tenant")
    ebay_oauth_service.disconnect(account, db)
    return {"status": "disconnected"}


@router.post("/sync-orders")
def sync_orders(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Pulls new orders from eBay's Fulfillment API and creates local orders
    from them. Safe to call repeatedly - already-imported eBay orders are
    skipped (see ebay_order_service for the duplicate-protection logic).
    """
    account = (
        db.query(EbayAccount)
        .filter(EbayAccount.tenant_id == current_user.tenant_id, EbayAccount.status == "CONNECTED")
        .first()
    )
    if not account:
        raise HTTPException(status_code=400, detail="Kein verbundenes eBay-Konto gefunden.")

    try:
        result = ebay_order_service.sync_orders(account, current_user.tenant_id, db)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"eBay-Synchronisierung fehlgeschlagen: {exc}")

    return result