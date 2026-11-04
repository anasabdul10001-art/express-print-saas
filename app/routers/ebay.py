import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import get_current_user, get_db
from app.models.ebay_account import EbayAccount
from app.models.user import User
from app.schemas.ebay import AuthorizeUrlOut, EbayAccountOut
from app.services import ebay_deletion_service, ebay_oauth_service, ebay_order_service

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


@router.get("/account-deletion")
def account_deletion_challenge(challenge_code: str):
    """eBay's ownership check, run when this endpoint is saved on its site."""
    if not settings.ebay_deletion_verification_token:
        raise HTTPException(status_code=503, detail="Verification token is not configured")
    return JSONResponse({"challengeResponse": ebay_deletion_service.challenge_response(challenge_code)})


@router.post("/account-deletion", status_code=204)
async def account_deletion_notification(
    request: Request,
    x_ebay_signature: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """
    eBay telling us a user asked for their data to be deleted. Unauthenticated
    by nature, so the signature is what stops anyone else triggering a delete.
    Deleting before acknowledging means a failure returns 500 and eBay retries,
    rather than the request being acknowledged and then lost.
    """
    body = await request.body()

    try:
        valid = ebay_deletion_service.signature_is_valid(body, x_ebay_signature)
    except ebay_deletion_service.PublicKeyUnavailable:
        # Our problem or eBay's, not a bad request: let eBay retry.
        raise HTTPException(status_code=503, detail="Could not verify the signature right now")
    if not valid:
        raise HTTPException(status_code=412, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if payload.get("metadata", {}).get("topic") == ebay_deletion_service.DELETION_TOPIC:
        data = payload.get("notification", {}).get("data", {})
        ebay_deletion_service.delete_user_data(data.get("username"), data.get("userId"), db)

    return Response(status_code=204)
