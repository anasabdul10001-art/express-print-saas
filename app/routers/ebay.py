import re
import json

import httpx

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import get_current_user, get_db
from app.models.ebay_account import EbayAccount
from app.models.order import Order
from app.models.product import Product
from app.models.user import User
from app.schemas.ebay import AuthorizeUrlOut, EbayAccountOut
from app.services import ebay_signing_service, ebay_deletion_service, ai_reply_service, ebay_extras_service, ebay_listing_service, ebay_oauth_service, ebay_order_service, ebay_stock_service, live_sync

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


@router.post("/live-sync")
def run_live_sync(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Called by the open pages: new orders, new products, pictures and quantities
    from eBay. Does nothing if a run happened a moment ago. eBay is only read."""
    return live_sync.run(db, current_user.tenant_id)


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

    total = {"created": 0, "products_created": 0, "skipped": 0, "skipped_reasons": [], "fees_updated": 0, "fee_notes": []}
    errors = []
    for account in accounts:
        try:
            with live_sync.lock_for(current_user.tenant_id):
                result = ebay_order_service.sync_orders(account, current_user.tenant_id, db)
        except ValueError as exc:
            errors.append(f"{account.ebay_user_id or 'eBay'}: {exc}")
            continue
        for key in ("created", "products_created", "skipped", "fees_updated"):
            total[key] += result.get(key, 0)
        total["skipped_reasons"] += result.get("skipped_reasons", [])
        total["fee_notes"] += result.get("fee_notes", [])

    if errors and len(errors) == len(accounts):
        raise HTTPException(status_code=502, detail=f"eBay-Synchronisierung fehlgeschlagen: {'; '.join(errors)}")
    if errors:
        total["skipped_reasons"] += errors
    return total


class PushPricesIn(BaseModel):
    product_ids: list[str] | None = None  # None = all active products
    dry_run: bool = True
    include_price: bool = True
    include_quantity: bool = False  # also send the ShipSync stock as the eBay available quantity


@router.post("/push-prices")
def push_prices(
    payload: PushPricesIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Sends the ShipSync selling price of products to their eBay listings
    (matched by SKU, else item number). dry_run=True only compares and
    changes nothing on eBay. Only listings whose price differs are touched."""
    accounts = _connected_accounts(current_user, db).all()
    if not accounts:
        raise HTTPException(status_code=400, detail="Kein verbundenes eBay-Konto gefunden.")

    query = db.query(Product).filter(Product.tenant_id == current_user.tenant_id, Product.active.is_(True),
                                     Product.unlisted.is_(False))
    if payload.product_ids is not None:
        if not payload.product_ids:
            raise HTTPException(status_code=400, detail="Keine Produkte ausgewählt.")
        query = query.filter(Product.id.in_(payload.product_ids))
    products = {p.sku: p for p in query.all()}

    changes, errors, matched = [], [], set()
    for account in accounts:
        name = account.ebay_user_id or "eBay"
        try:
            listings = ebay_listing_service.fetch_active_listings(account, db)
        except (ValueError, httpx.HTTPError) as exc:
            errors.append(f"{name}: {exc}")
            continue
        todo = []
        for listing in listings:
            product = products.get(listing["sku"] or "") or products.get(listing["item_id"])
            if not product:
                continue
            matched.add(product.sku)
            new_price = None
            if payload.include_price and product.selling_price is not None and listing["price"] is not None:
                candidate = Decimal(product.selling_price).quantize(Decimal("0.01"))
                if listing["price"] != candidate:
                    new_price = candidate
            new_qty = None
            if payload.include_quantity and product.stock_quantity is not None and listing["available"] != product.stock_quantity:
                new_qty = max(0, int(product.stock_quantity))
            if new_price is None and new_qty is None:
                continue
            todo.append((listing["item_id"], new_price, new_qty))
            changes.append({"account": name, "item_id": listing["item_id"], "sku": product.sku,
                            "title": product.title, "ebay_price": str(listing["price"]),
                            "new_price": str(new_price) if new_price is not None else None,
                            "ebay_quantity": listing["available"], "new_quantity": new_qty, "result": None})
        if payload.dry_run or not todo:
            continue
        if len(todo) > 200:
            raise HTTPException(status_code=400, detail="Maximal 200 Angebote pro Vorgang.")
        try:
            results = ebay_listing_service.revise_listings(account, db, todo)
        except (ValueError, httpx.HTTPError) as exc:
            errors.append(f"{name}: {exc}")
            continue
        for change in changes:
            if change["account"] == name and change["item_id"] in results:
                change["result"] = "ok" if results[change["item_id"]] is None else results[change["item_id"]]

    return {
        "applied": not payload.dry_run,
        "to_change": len(changes),
        "updated": sum(1 for c in changes if c["result"] == "ok"),
        "failed": sum(1 for c in changes if c["result"] not in (None, "ok")),
        "no_ebay_match": len([s for s, p in products.items() if s not in matched and (
            (payload.include_price and p.selling_price is not None) or payload.include_quantity)]),
        "items": changes,
        "errors": errors,
    }


@router.get("/listing-info/{item_id}")
def listing_info(
    item_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Read-only: what eBay says about one listing (variations, SKUs, quantities)."""
    if not item_id.isdigit():
        raise HTTPException(status_code=400, detail="Ungültige Artikelnummer.")
    accounts = _connected_accounts(current_user, db).all()
    if not accounts:
        raise HTTPException(status_code=400, detail="Kein verbundenes eBay-Konto gefunden.")
    errors = []
    for account in accounts:
        try:
            return ebay_listing_service.get_item_variations(account, db, item_id)
        except (ValueError, httpx.HTTPError) as exc:
            errors.append(str(exc))
    raise HTTPException(status_code=502, detail="; ".join(errors))


@router.get("/orders-raw")
def orders_raw(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Read-only diagnostic: the money fields eBay returns per order (prices, discounts,
    shipping, refunds, payment summary), to reconcile ShipSync's accounting with eBay."""
    accounts = _connected_accounts(current_user, db).all()
    if not accounts:
        raise HTTPException(status_code=400, detail="Kein verbundenes eBay-Konto gefunden.")
    out = []
    for account in accounts:
        token = ebay_oauth_service.get_valid_access_token(account, db)
        resp = httpx.get(
            f"{ebay_order_service.EBAY_API_BASE[account.environment]}/sell/fulfillment/v1/order",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            params={"limit": 50}, timeout=20.0,
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"eBay: {resp.status_code} {resp.text[:200]}")
        for o in resp.json().get("orders", []):
            out.append({
                "orderId": o.get("orderId"),
                "creationDate": o.get("creationDate"),
                "pricingSummary": o.get("pricingSummary"),
                "paymentSummary": o.get("paymentSummary"),
                "orderPaymentStatus": o.get("orderPaymentStatus"),
                "cancelStatus": (o.get("cancelStatus") or {}).get("cancelState"),
                "lineItems": [
                    {"quantity": li.get("quantity"), "lineItemCost": li.get("lineItemCost"),
                     "total": li.get("total"), "discountedLineItemCost": li.get("discountedLineItemCost"),
                     "appliedPromotions": li.get("appliedPromotions"), "deliveryCost": li.get("deliveryCost")}
                    for li in o.get("lineItems", [])
                ],
            })
    return out


class VariationQuantitiesIn(BaseModel):
    product_id: str
    available: list[int]  # one number per variation, in the order /ebay/listing-info returns them


@router.post("/variation-quantities")
def variation_quantities(
    payload: VariationQuantitiesIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Sets the per-variation (e.g. colour) quantities of a product's eBay listing and stores
    their sum as the product's stock in ShipSync."""
    if any(q < 0 for q in payload.available):
        raise HTTPException(status_code=400, detail="Mengen dürfen nicht negativ sein.")
    product = db.query(Product).filter(Product.id == payload.product_id, Product.tenant_id == current_user.tenant_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Produkt nicht gefunden.")
    accounts = _connected_accounts(current_user, db).all()
    if not accounts:
        raise HTTPException(status_code=400, detail="Kein verbundenes eBay-Konto gefunden.")
    item_id = product.sku if product.sku.isdigit() else None
    errors = []
    for account in accounts:
        try:
            if item_id is None:
                match = next((l for l in ebay_listing_service.fetch_active_listings(account, db) if l["sku"] == product.sku), None)
                if not match:
                    continue
                item_id = match["item_id"]
            ebay_listing_service.revise_variation_quantities(account, db, item_id, payload.available)
            product.stock_quantity = sum(payload.available)
            db.commit()
            return {"item_id": item_id, "stock_quantity": product.stock_quantity}
        except (ValueError, httpx.HTTPError) as exc:
            errors.append(str(exc))
    raise HTTPException(status_code=502, detail="; ".join(errors) or "Kein passendes eBay-Angebot gefunden.")


@router.get("/finances-raw")
def finances_raw(
    kind: str = "SHIPPING_LABEL",
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Read-only diagnostic: raw Finances transactions of one type (SALE, REFUND, SHIPPING_LABEL,
    NON_SALE_CHARGE ...) to see how label costs and extra fees are reported."""
    if not kind.replace("_", "").isalpha():
        raise HTTPException(status_code=400, detail="Ungültiger Typ.")
    accounts = _connected_accounts(current_user, db).all()
    if not accounts:
        raise HTTPException(status_code=400, detail="Kein verbundenes eBay-Konto gefunden.")
    out = []
    for account in accounts:
        token = ebay_oauth_service.get_valid_access_token(account, db)
        url = f"{ebay_order_service.FINANCES_API_BASE[account.environment]}/sell/finances/v1/transaction"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        headers.update(ebay_signing_service.signed_headers(account, db, "GET", url))
        resp = httpx.get(url, headers=headers, params={"filter": f"transactionType:{{{kind}}}", "limit": 200}, timeout=20.0)
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"eBay: {resp.status_code} {resp.text[:300]}")
        out += resp.json().get("transactions", [])
    return out


@router.get("/subscription-raw")
def subscription_raw(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Read-only diagnostic: does this eBay account have an eBay Store subscription? (Account API
    getSubscription.) Shows eBay's answer as it is, per connected account."""
    accounts = _connected_accounts(current_user, db).all()
    if not accounts:
        raise HTTPException(status_code=400, detail="Kein verbundenes eBay-Konto gefunden.")
    out = []
    for account in accounts:
        try:
            result = ebay_extras_service.subscriptions(account, db)
        except (ValueError, httpx.HTTPError) as exc:
            result = {"ok": False, "error": str(exc)[:300]}
        out.append({"ebay_user_id": account.ebay_user_id, **result})
    return out


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
            with live_sync.lock_for(current_user.tenant_id):
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


class PullStockIn(BaseModel):
    dry_run: bool = True
    # {product_id: eBay quantity shown in the preview}; only these are changed
    confirmed: dict[str, int] = Field(default_factory=dict)


@router.post("/pull-stock")
def pull_stock(
    payload: PullStockIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Takes eBay's available quantities over into ShipSync. Nothing is changed
    on eBay. New orders are imported first: eBay has already subtracted them, so
    importing them afterwards would subtract them a second time. If anything
    cannot be read from eBay, no quantity is taken over."""
    accounts = _connected_accounts(current_user, db).all()
    if not accounts:
        raise HTTPException(status_code=400, detail="Kein verbundenes eBay-Konto gefunden.")
    if not payload.dry_run and not payload.confirmed:
        raise HTTPException(status_code=400, detail="Keine Produkte ausgewählt.")

    listings, new_orders = [], 0
    for account in accounts:
        name = account.ebay_user_id or "eBay"
        try:
            with live_sync.lock_for(current_user.tenant_id):
                new_orders += ebay_order_service.sync_orders(account, current_user.tenant_id, db)["created"]
            listings += ebay_listing_service.fetch_active_listings(account, db)
        except (ValueError, httpx.HTTPError) as exc:
            raise HTTPException(status_code=502, detail=f"eBay-Abruf fehlgeschlagen, keine Menge übernommen. {name}: {exc}")

    result = ebay_stock_service.compare(db, current_user.tenant_id, listings)
    updated, outdated = 0, []
    if not payload.dry_run:
        updated, outdated = ebay_stock_service.apply(db, current_user.tenant_id, result["items"], payload.confirmed)
    return {**result, "applied": not payload.dry_run, "updated": updated, "outdated": outdated,
            "new_orders": new_orders}


def _collect(current_user: User, db: Session, fn):
    """Runs fn(account, db) for every connected account; one broken account
    or missing scope never hides the data of the others."""
    accounts = _connected_accounts(current_user, db).all()
    if not accounts:
        raise HTTPException(status_code=400, detail="Kein verbundenes eBay-Konto gefunden.")
    results, errors = [], []
    for account in accounts:
        try:
            results.append((account.ebay_user_id or "eBay", fn(account, db)))
        except (ValueError, httpx.HTTPError) as exc:
            errors.append(f"{account.ebay_user_id or 'eBay'}: {exc}")
    if errors and not results:
        raise HTTPException(status_code=502, detail="; ".join(errors))
    return results, errors


@router.get("/shipping-policies")
def get_shipping_policies(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    results, errors = _collect(current_user, db, ebay_extras_service.shipping_policies)
    return {"accounts": [{"account": n, "policies": p} for n, p in results], "errors": errors}


@router.get("/traffic")
def get_traffic(days: int = 30, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    results, errors = _collect(current_user, db, lambda a, d: ebay_extras_service.traffic(a, d, min(max(days, 1), 90)))
    return {"accounts": [{"account": n, "listings": rows} for n, rows in results], "errors": errors}


@router.get("/cases")
def get_cases(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    results, errors = _collect(current_user, db, ebay_extras_service.cases)
    items = [dict(i, account=n) for n, r in results for i in r["items"]]
    for _, r in results:
        errors += r["errors"]
    return {"items": items, "errors": errors}


@router.get("/messages")
def get_messages(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    results, errors = _collect(current_user, db, ebay_extras_service.messages)
    items = [dict(m, account=n) for n, msgs in results for m in msgs]
    return {"items": items, "errors": errors, "ai_configured": ai_reply_service.configured()}


class SuggestReplyIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    buyer_name: str | None = None  # only used to mask it, never sent


@router.post("/suggest-reply")
def suggest_reply(body: SuggestReplyIn, current_user: User = Depends(get_current_user)):
    """Drafts a German reply (never sends anything). Only the masked message text
    goes to the AI service - no order, item or address data."""
    if re.search(r"<\s*(?:!doctype|html|head|body|table|div|style)\b", body.message, re.I):
        raise HTTPException(status_code=422, detail="Bitte nur den Nachrichtentext einfügen, keine HTML-E-Mail.")
    try:
        return ai_reply_service.suggest_reply(body.message, [body.buyer_name] if body.buyer_name else None)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


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
