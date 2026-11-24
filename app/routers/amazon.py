import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import get_current_user, get_db
from app.models.amazon_account import AmazonAccount
from app.models.order import Order
from app.models.user import User
from app.schemas.amazon import AmazonAccountOut, AmazonAuthorizeUrlOut
from app.services import amazon_oauth_service, amazon_service, ebay_order_service

router = APIRouter(prefix="/amazon", tags=["amazon"])


def _connected(current_user: User, db: Session):
    # DISCONNECTED rows stay in the table (token nulled), so filter them out.
    return (
        db.query(AmazonAccount)
        .filter(AmazonAccount.tenant_id == current_user.tenant_id, AmazonAccount.status == "CONNECTED")
        .order_by(AmazonAccount.connected_at)
    )


@router.get("/status")
def status(current_user: User = Depends(get_current_user)):
    return {
        "configured": amazon_oauth_service.configured(),
        "consent_flow": amazon_oauth_service.consent_flow_configured(),
    }


@router.get("/authorize-url", response_model=AmazonAuthorizeUrlOut)
def authorize_url(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    try:
        return AmazonAuthorizeUrlOut(authorize_url=amazon_oauth_service.build_authorize_url(current_user.tenant_id, db))
    except ValueError:
        raise HTTPException(status_code=503, detail="Amazon ist noch nicht eingerichtet (AMAZON_LWA_CLIENT_ID / _SECRET / AMAZON_APPLICATION_ID fehlen).")


@router.get("/callback")
def callback(request: Request, db: Session = Depends(get_db)):
    """Amazon redirects the seller's browser here. No JWT is sent, so the
    request is tied to a tenant only through our one-time state."""
    page = f"{settings.frontend_url}/app/dashboard.html"
    query = dict(request.query_params)
    if query.get("error"):
        return RedirectResponse(f"{page}?amazon_error={query['error']}")
    try:
        account = amazon_oauth_service.handle_callback(query, db)
    except (ValueError, httpx.HTTPError) as exc:
        return RedirectResponse(f"{page}?amazon_error={str(exc).split(':')[0].split(' ')[0]}")
    return RedirectResponse(f"{page}?amazon_connected={account.seller_id}")


class ConnectTokenIn(BaseModel):
    seller_id: str
    refresh_token: str


@router.post("/connect-token", response_model=AmazonAccountOut)
def connect_token(body: ConnectTokenIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Own-account shortcut: the refresh token Seller Central shows after
    "Authorize app" (self-authorization). It is checked against Amazon before
    it is stored encrypted."""
    try:
        return amazon_oauth_service.connect_with_refresh_token(current_user.tenant_id, body.seller_id, body.refresh_token, db)
    except ValueError as exc:
        if str(exc) == "amazon_not_configured":
            raise HTTPException(status_code=503, detail="Amazon ist noch nicht eingerichtet (AMAZON_LWA_CLIENT_ID / _SECRET fehlen).")
        raise HTTPException(status_code=400, detail="Amazon hat das Token abgelehnt - gehört es zu dieser App?")
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Amazon ist nicht erreichbar.")


@router.get("/accounts", response_model=list[AmazonAccountOut])
def accounts(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _connected(current_user, db).all()


@router.post("/disconnect")
def disconnect(account_id: str | None = None, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    query = db.query(AmazonAccount).filter(AmazonAccount.tenant_id == current_user.tenant_id)
    if account_id:
        query = query.filter(AmazonAccount.id == account_id)
    rows = query.all()
    if not rows:
        raise HTTPException(status_code=404, detail="Kein Amazon-Konto verbunden.")
    for row in rows:
        amazon_oauth_service.disconnect(row, db)
    return {"status": "disconnected"}


def _run_for_all(current_user: User, db: Session, fn, label: str):
    rows = _connected(current_user, db).all()
    if not rows:
        raise HTTPException(status_code=400, detail="Kein verbundenes Amazon-Konto gefunden.")
    results, errors = [], []
    for account in rows:
        try:
            results.append(fn(account))
        except (ValueError, httpx.HTTPError) as exc:
            db.rollback()
            errors.append(f"{account.seller_id}: {exc}")
    if errors and not results:
        raise HTTPException(status_code=502, detail=f"Amazon-{label} fehlgeschlagen: {'; '.join(errors)}")
    return results, errors


@router.post("/sync-orders")
def sync_orders(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Pulls new/updated orders of every connected account. Safe to repeat."""
    results, errors = _run_for_all(
        current_user, db, lambda a: amazon_service.sync_orders(a, current_user.tenant_id, db), "Synchronisierung")
    total = {"created": 0, "products_created": 0, "skipped": 0, "skipped_reasons": [], "fee_notes": []}
    for r in results:
        for key in ("created", "products_created", "skipped"):
            total[key] += r[key]
        total["skipped_reasons"] += r["skipped_reasons"]
        total["fee_notes"] += r["notes"]
    total["skipped_reasons"] += errors
    return total


@router.post("/import-products")
def import_products(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    results, errors = _run_for_all(
        current_user, db, lambda a: amazon_service.import_products(a, current_user.tenant_id, db), "Import")
    total = {"listings_found": 0, "products_created": 0, "already_known": 0}
    for r in results:
        for key in total:
            total[key] += r[key]
    total["errors"] = errors
    return total


class ShipOrderIn(BaseModel):
    tracking_number: str
    carrier: str = "DHL"


@router.post("/orders/{order_id}/ship")
def ship_order(order_id: str, body: ShipOrderIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Confirms the shipment with tracking at Amazon and marks the order shipped."""
    if body.carrier not in ebay_order_service.SHIPPING_CARRIERS:
        raise HTTPException(status_code=400, detail="Unbekannter Paketdienst.")
    order = db.query(Order).filter(Order.id == order_id, Order.tenant_id == current_user.tenant_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    account = (
        _connected(current_user, db).filter(AmazonAccount.id == order.amazon_account_id).first()
        if order.amazon_account_id else None
    )
    if not account:
        raise HTTPException(status_code=400, detail="Kein verbundenes Amazon-Konto für diese Bestellung gefunden.")
    try:
        amazon_service.mark_order_shipped(account, order, body.tracking_number, body.carrier, db)
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=f"Amazon-Versandmeldung fehlgeschlagen: {exc}")
    return {"status": order.status, "tracking_number": order.tracking_number}
