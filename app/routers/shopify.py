import uuid
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import get_current_user, get_db
from app.models.order import Order
from app.models.shopify_account import ShopifyAccount
from app.models.product import Product
from app.models.user import User
from app.schemas.shopify import ShopifyAccountOut, ShopifyAuthorizeUrlOut
from app.services import ebay_order_service, shopify_oauth_service, shopify_service

router = APIRouter(prefix="/shopify", tags=["shopify"])


def _connected(current_user: User, db: Session):
    # DISCONNECTED rows stay in the table (token nulled), so filter them out.
    return (
        db.query(ShopifyAccount)
        .filter(ShopifyAccount.tenant_id == current_user.tenant_id, ShopifyAccount.status == "CONNECTED")
        .order_by(ShopifyAccount.connected_at)
    )


def _name(account: ShopifyAccount) -> str:
    return account.shop_name or account.shop_domain


@router.get("/authorize-url", response_model=ShopifyAuthorizeUrlOut)
def authorize_url(shop: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """The consent-screen URL for the given store ("my-store" or "my-store.myshopify.com")."""
    try:
        return ShopifyAuthorizeUrlOut(authorize_url=shopify_oauth_service.build_authorize_url(current_user.tenant_id, shop, db))
    except ValueError as exc:
        if str(exc) == "shopify_not_configured":
            raise HTTPException(status_code=503, detail="Shopify ist noch nicht eingerichtet (SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET fehlen).")
        raise HTTPException(status_code=400, detail="Ungültige Shop-Adresse. Erwartet: dein-shop.myshopify.com")


@router.get("/callback")
def callback(request: Request, db: Session = Depends(get_db)):
    """Shopify redirects the merchant's browser here. No JWT is sent, so the
    request is authenticated by Shopify's HMAC signature and our one-time state."""
    page = f"{settings.frontend_url}/app/dashboard.html"
    query = dict(request.query_params)
    if query.get("error"):
        return RedirectResponse(f"{page}?shopify_error={query['error']}")
    try:
        account = shopify_oauth_service.handle_callback(query, db)
    except (ValueError, httpx.HTTPError) as exc:
        return RedirectResponse(f"{page}?shopify_error={str(exc).split(':')[0]}")
    return RedirectResponse(f"{page}?shopify_connected={account.shop_domain}")


@router.get("/status")
def status(current_user: User = Depends(get_current_user)):
    return {"configured": shopify_oauth_service.configured()}


@router.get("/accounts", response_model=list[ShopifyAccountOut])
def accounts(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return _connected(current_user, db).all()


@router.post("/disconnect")
def disconnect(account_id: str | None = None, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    query = db.query(ShopifyAccount).filter(ShopifyAccount.tenant_id == current_user.tenant_id)
    if account_id:
        query = query.filter(ShopifyAccount.id == account_id)
    rows = query.all()
    if not rows:
        raise HTTPException(status_code=404, detail="Kein Shopify-Shop verbunden.")
    for row in rows:
        shopify_oauth_service.disconnect(row, db)
    return {"status": "disconnected"}


def _run_for_all(current_user: User, db: Session, fn, label: str):
    rows = _connected(current_user, db).all()
    if not rows:
        raise HTTPException(status_code=400, detail="Kein verbundener Shopify-Shop gefunden.")
    results, errors = [], []
    for account in rows:
        try:
            results.append(fn(account))
        except (ValueError, httpx.HTTPError) as exc:
            db.rollback()
            errors.append(f"{_name(account)}: {exc}")
    if errors and not results:
        raise HTTPException(status_code=502, detail=f"Shopify-{label} fehlgeschlagen: {'; '.join(errors)}")
    return results, errors


@router.post("/sync-orders")
def sync_orders(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Pulls the latest orders of every connected store. Safe to repeat:
    already-imported orders are skipped (refunds/fees are refreshed)."""
    results, errors = _run_for_all(
        current_user, db, lambda a: shopify_service.sync_orders(a, current_user.tenant_id, db), "Synchronisierung")
    total = {"created": 0, "products_created": 0, "skipped": 0, "skipped_reasons": [], "fees_updated": 0, "fee_notes": []}
    for r in results:
        for key in ("created", "products_created", "skipped"):
            total[key] += r[key]
        total["skipped_reasons"] += r["skipped_reasons"]
        total["fee_notes"] += r["notes"]
    total["skipped_reasons"] += errors
    return total


@router.post("/import-products")
def import_products(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Creates the missing products from every connected store's catalogue."""
    results, errors = _run_for_all(
        current_user, db, lambda a: shopify_service.import_products(a, current_user.tenant_id, db), "Import")
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
    """Creates the fulfillment (with tracking) at Shopify and marks the order shipped."""
    if body.carrier not in ebay_order_service.SHIPPING_CARRIERS:
        raise HTTPException(status_code=400, detail="Unbekannter Paketdienst.")
    order = db.query(Order).filter(Order.id == order_id, Order.tenant_id == current_user.tenant_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    accounts_q = _connected(current_user, db)
    account = accounts_q.filter(ShopifyAccount.id == order.shopify_account_id).first() if order.shopify_account_id else None
    if not account:
        raise HTTPException(status_code=400, detail="Kein verbundener Shopify-Shop für diese Bestellung gefunden.")
    try:
        shopify_service.mark_order_shipped(account, order, body.tracking_number, body.carrier, db)
    except (ValueError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=502, detail=f"Shopify-Versandmeldung fehlgeschlagen: {exc}")
    return {"status": order.status, "tracking_number": order.tracking_number}


class PushIn(BaseModel):
    product_ids: list[uuid.UUID] | None = None
    include_price: bool = True
    include_quantity: bool = False
    # True only compares. Nothing is written to the store unless this is False.
    dry_run: bool = True


@router.post("/push-listings")
def push_listings(payload: PushIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Sends NettoDesk prices / stock to the connected stores (matched by SKU)."""
    query = db.query(Product).filter(Product.tenant_id == current_user.tenant_id, Product.active.is_(True),
                                     Product.unlisted.is_(False))
    if payload.product_ids is not None:
        query = query.filter(Product.id.in_(payload.product_ids))
    products = {p.sku: p for p in query.all()}

    def run(account):
        changes = shopify_service.plan_push(account, products, payload.include_price, payload.include_quantity)
        if len(changes) > 200:
            raise ValueError("Maximal 200 Änderungen pro Vorgang.")
        if not payload.dry_run:
            shopify_service.apply_push(account, changes)
        return [{**c, "shop": _name(account)} for c in changes]

    results, errors = _run_for_all(current_user, db, run, "Abgleich")
    items = [{k: v for k, v in change.items() if not k.endswith("_id")} for group in results for change in group]
    return {
        "applied": not payload.dry_run, "to_change": len(items),
        "updated": 0 if payload.dry_run else sum(1 for c in items if not c["errors"]),
        "failed": sum(1 for c in items if c["errors"]), "items": items, "errors": errors,
    }
