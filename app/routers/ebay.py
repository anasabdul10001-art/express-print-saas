import json

import httpx

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import get_current_user, get_db
from app.models.ebay_account import EbayAccount
from app.models.order import Order
from app.models.user import User
from app.schemas.ebay import AuthorizeUrlOut, EbayAccountOut
from app.services import ebay_deletion_service, ebay_listing_service, ebay_oauth_service, ebay_order_service

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
    """The first connected account (kept for older clients; see /accounts)."""
    return _connected_accounts(current_user, db).first()


@router.get("/accounts", response_model=list[EbayAccountOut])
def list_accounts(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """All eBay accounts currently CONNECTED for this tenant."""
    return _connected_accounts(current_user, db).all()


def _connected_accounts(current_user: User, db: Session):
    # DISCONNECTED rows stay in the table (tokens nulled), so filter them out.
    return (
        db.query(EbayAccount)
        .filter(EbayAccount.tenant_id == current_user.tenant_id, EbayAccount.status == "CONNECTED")
        .order_by(EbayAccount.connected_at)
    )


@router.post("/disconnect")
def disconnect_ebay(
    account_id: str | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Disconnects one account (account_id), or every account when omitted."""
    query = db.query(EbayAccount).filter(EbayAccount.tenant_id == current_user.tenant_id)
    if account_id:
        query = query.filter(EbayAccount.id == account_id)
    accounts = query.all()
    if not accounts:
        raise HTTPException(status_code=404, detail="No eBay account connected for this tenant")
    for account in accounts:
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
    accounts = _connected_accounts(current_user, db).all()
    if not accounts:
        raise HTTPException(status_code=400, detail="Kein verbundenes eBay-Konto gefunden.")

    total = {"created": 0, "products_created": 0, "skipped": 0, "skipped_reasons": [], "fees_updated": 0}
    errors = []
    for account in accounts:
        try:
            result = ebay_order_service.sync_orders(account, current_user.tenant_id, db)
        except ValueError as exc:
            errors.append(f"{account.ebay_user_id or 'eBay'}: {exc}")
            continue
        for key in ("created", "products_created", "skipped", "fees_updated"):
            total[key] += result.get(key, 0)
        total["skipped_reasons"] += result.get("skipped_reasons", [])

    if errors and len(errors) == len(accounts):
        raise HTTPException(status_code=502, detail=f"eBay-Synchronisierung fehlgeschlagen: {'; '.join(errors)}")
    if errors:
        total["skipped_reasons"] += errors
    return total


@router.post("/import-products")
def import_products(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Creates missing products from the active listings of every connected
    eBay account. Existing products are left untouched."""
    accounts = _connected_accounts(current_user, db).all()
    if not accounts:
        raise HTTPException(status_code=400, detail="Kein verbundenes eBay-Konto gefunden.")

    total = {"listings_found": 0, "products_created": 0, "already_known": 0}
    errors = []
    for account in accounts:
        try:
            result = ebay_listing_service.import_listings(account, current_user.tenant_id, db)
        except (ValueError, httpx.HTTPError) as exc:
            errors.append(f"{account.ebay_user_id or 'eBay'}: {exc}")
            continue
        for key in total:
            total[key] += result[key]
    if errors and len(errors) == len(accounts):
        raise HTTPException(status_code=502, detail=f"eBay-Import fehlgeschlagen: {'; '.join(errors)}")
    total["errors"] = errors
    return total


class ShipOrderIn(BaseModel):
    tracking_number: str
    carrier: str = "DHL"


@router.post("/orders/{order_id}/ship")
def ship_order(
    order_id: str,
    body: ShipOrderIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Reports tracking number + carrier to eBay and marks the order shipped."""
    if body.carrier not in ebay_order_service.SHIPPING_CARRIERS:
        raise HTTPException(status_code=400, detail="Unbekannter Paketdienst.")
    order = db.query(Order).filter(Order.id == order_id, Order.tenant_id == current_user.tenant_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    # The order knows which eBay account it came from; fall back to any connected one.
    accounts = _connected_accounts(current_user, db)
    account = accounts.filter(EbayAccount.id == order.ebay_account_id).first() if order.ebay_account_id else None
    account = account or accounts.first()
    if not account:
        raise HTTPException(status_code=400, detail="Kein verbundenes eBay-Konto gefunden.")
    try:
        ebay_order_service.mark_order_shipped(account, order, body.tracking_number, body.carrier, db)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"eBay-Versandmeldung fehlgeschlagen: {exc}")
    return {"status": order.status, "tracking_number": order.tracking_number}


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
