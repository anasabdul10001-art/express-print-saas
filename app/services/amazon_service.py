"""
Amazon SP-API: import listings, sync orders (+ fees) and confirm shipments -
the same jobs the eBay and Shopify services do.

Endpoints used (from Amazon's published OpenAPI models):
    GET  /listings/2021-08-01/items/{sellerId}         listings (page token = pagination.nextToken)
    GET  /orders/2026-01-01/orders                     orders (Orders API v2026-01-01, no RDT needed)
    GET  /orders/2026-01-01/orders/{orderId}           one order (to get its item ids)
    GET  /finances/2024-06-19/transactions             fees, filtered by ORDER_ID
    POST /orders/v0/orders/{orderId}/shipmentConfirmation   tracking (the 2026 API has no equivalent yet)

Notes worth knowing:
    - Orders become the same Order/OrderItem rows as eBay ones (external_order_ref
      "amazon:<AmazonOrderId>", Order.amazon_account_id). Pending orders (payment
      not confirmed) are skipped and picked up on a later sync.
    - Buyer name and address are PII: they only come back if the app's developer
      profile has been approved for the restricted roles. Without them the order
      is imported without an address (no label), never rejected.
    - searchOrders is heavily rate limited (~1 request / 3 minutes sustained), so a
      429 is reported as a note and is not an error. The first sync reads the last
      30 days; later syncs only ask for orders updated since the previous sync.
    - Fees come from the Finances API, which lags the sale by ~2 days: orders
      without a fee are retried on later syncs. Unverified detail: the top-level
      breakdown named "Expenses" is taken as the order's Amazon fees; if it is
      not there the fee stays empty (never guessed). Refunds are not read yet.
    - Stock: FBA orders (fulfilled by Amazon) and orders placed before the account
      was connected never touch ShipSync stock; everything else decrements it.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models.amazon_account import AmazonAccount
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.services import stock_listing_service
from app.models.stock_movement import StockMovement
from app.services import amazon_oauth_service

REF_PREFIX = "amazon:"
FIRST_SYNC_DAYS = 30
FEE_LOOKUPS_PER_SYNC = 15
FEE_LOOKUP_MIN_AGE_DAYS = 3
FEE_LOOKUP_MAX_AGE_DAYS = 60


class AmazonRateLimited(ValueError):
    """Amazon answered 429 (quota exhausted); try again later."""


def call(account: AmazonAccount, db: Session, method: str, path: str, params=None, json=None):
    token = amazon_oauth_service.get_access_token(account, db)
    resp = httpx.request(
        method, f"{settings.amazon_sp_api_host}{path}", params=params, json=json,
        headers={"x-amz-access-token": token, "Content-Type": "application/json"}, timeout=30.0,
    )
    if resp.status_code == 429:
        raise AmazonRateLimited("Amazon: Anfragelimit erreicht, bitte in ein paar Minuten erneut versuchen.")
    if resp.status_code in (401, 403):
        raise ValueError(f"amazon_unauthorized ({resp.status_code}): {resp.text[:200]}")
    if resp.status_code not in (200, 204):
        raise ValueError(f"amazon_request_failed ({resp.status_code}): {resp.text[:300]}")
    return resp.json() if resp.status_code == 200 and resp.content else {}


def _dec(value) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------- products

def fetch_listings(account: AmazonAccount, db: Session, max_pages: int = 50) -> list[dict]:
    """The seller's listings as flat dicts (Amazon pages through at most 1000 results)."""
    rows, token = [], None
    for _ in range(max_pages):
        params = {"marketplaceIds": account.marketplace_id, "includedData": "summaries,offers,fulfillmentAvailability", "pageSize": 20}
        if token:
            params["pageToken"] = token
        data = call(account, db, "GET", f"/listings/2021-08-01/items/{account.seller_id}", params=params)
        for item in data.get("items") or []:
            summary = next((s for s in item.get("summaries") or [] if s.get("marketplaceId") == account.marketplace_id), None) \
                or next(iter(item.get("summaries") or []), {})
            offer = next((o for o in item.get("offers") or [] if o.get("marketplaceId") == account.marketplace_id), None) \
                or next(iter(item.get("offers") or []), {})
            availability = next(iter(item.get("fulfillmentAvailability") or []), {})
            rows.append({
                "sku": item.get("sku"),
                "title": (summary.get("itemName") or item.get("sku") or "")[:500],
                "price": _dec((offer.get("price") or {}).get("amount")),
                "stock": availability.get("quantity"),
                "image_url": (summary.get("mainImage") or {}).get("link"),
            })
        token = (data.get("pagination") or {}).get("nextToken")
        if not token:
            break
    return rows


def import_products(account: AmazonAccount, tenant_id: UUID, db: Session) -> dict:
    """Creates the missing products (SKU = Amazon seller SKU). Existing ones stay untouched."""
    listings = fetch_listings(account, db)
    known = {sku for (sku,) in db.query(Product.sku).filter(Product.tenant_id == tenant_id).all()}
    stock_listing_service.seen_in_shop(db, tenant_id, [
        (row["sku"], row["image_url"], row["price"]) for row in listings if row["sku"]])
    created = 0
    for row in listings:
        if not row["sku"] or row["sku"] in known:
            continue
        known.add(row["sku"])
        stock = row["stock"] if isinstance(row["stock"], int) else 0
        db.add(Product(
            tenant_id=tenant_id, sku=row["sku"], title=row["title"] or row["sku"],
            selling_price=row["price"], stock_quantity=max(0, stock), image_url=row["image_url"],
        ))
        created += 1
    db.commit()
    return {"listings_found": len(listings), "products_created": created, "already_known": len(listings) - created}


# ------------------------------------------------------------------ orders

INCLUDED = "BUYER,RECIPIENT,PROCEEDS,FULFILLMENT,CANCELLATION,PACKAGES"


def fetch_orders(account: AmazonAccount, db: Session, max_pages: int = 10) -> list[dict]:
    now = datetime.now(timezone.utc)
    params = {"marketplaceIds": account.marketplace_id, "includedData": INCLUDED, "maxResultsPerPage": 100}
    if account.last_sync_at:
        params["lastUpdatedAfter"] = (account.last_sync_at - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        params["createdAfter"] = (now - timedelta(days=FIRST_SYNC_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    orders, token = [], None
    for _ in range(max_pages):
        page_params = dict(params, paginationToken=token) if token else params
        data = call(account, db, "GET", "/orders/2026-01-01/orders", params=page_params)
        orders += data.get("orders") or []
        token = (data.get("pagination") or {}).get("nextToken")
        if not token:
            break
    return orders


def _ship_to(order: dict) -> dict:
    a = (order.get("recipient") or {}).get("deliveryAddress") or {}
    if not a:
        return {}
    return {
        "recipient_name": a.get("name"), "recipient_street1": a.get("addressLine1"),
        "recipient_street2": ", ".join(x for x in (a.get("addressLine2"), a.get("addressLine3")) if x) or None,
        "recipient_city": a.get("city"), "recipient_state": a.get("stateOrRegion"),
        "recipient_zip": a.get("postalCode"), "recipient_country_code": a.get("countryCode"),
        "recipient_phone": a.get("phone"),
    }


def _local_status(order: dict) -> str | None:
    """None = not importable yet (payment pending)."""
    fulfillment = order.get("fulfillment") or {}
    status = fulfillment.get("fulfillmentStatus")
    if status in ("PENDING", "PENDING_AVAILABILITY"):
        return None
    if status in ("CANCELLED", "UNFULFILLABLE"):
        return "canceled"
    if status == "SHIPPED" or fulfillment.get("fulfilledBy") == "AMAZON":
        return "shipped"
    return "new"


def sync_orders(account: AmazonAccount, tenant_id: UUID, db: Session) -> dict:
    notes: list[str] = []
    try:
        amazon_orders = fetch_orders(account, db)
    except AmazonRateLimited as exc:
        return {"created": 0, "products_created": 0, "skipped": 0, "skipped_reasons": [], "notes": [str(exc)]}

    created = products_created = skipped = 0
    skipped_reasons: list[str] = []
    connected_at = account.connected_at

    for ao in amazon_orders:
        number = ao.get("orderId")
        status = _local_status(ao)
        if not number or status is None:
            continue
        ref = REF_PREFIX + number

        existing = db.query(Order).filter(Order.tenant_id == tenant_id, Order.external_order_ref == ref).first()
        if existing:
            # Cancelled/shipped at Amazon after we imported it: follow Amazon.
            if existing.status in ("new", "ready_to_print") and status in ("canceled", "shipped"):
                existing.status = status
            continue

        lines, ok = [], True
        for item in ao.get("orderItems") or []:
            product_info = item.get("product") or {}
            quantity = int(item.get("quantityOrdered") or 1)
            sku = product_info.get("sellerSku") or product_info.get("asin")
            if not sku:
                ok = False
                skipped_reasons.append(f"Bestellung {number}: Position ohne SKU und ohne ASIN.")
                break
            price = _dec(((product_info.get("price") or {}).get("unitPrice") or {}).get("amount"))
            product = db.query(Product).filter(Product.tenant_id == tenant_id, Product.sku == sku).first()
            if not product:
                product = Product(
                    tenant_id=tenant_id, sku=sku, title=(product_info.get("title") or sku)[:255],
                    selling_price=price, stock_quantity=0,
                )
                db.add(product)
                db.flush()
                products_created += 1
            lines.append((product, quantity, price))
        if not ok:
            skipped += 1
            continue

        ordered_at = _parse_date(ao.get("createdTime"))
        is_fba = (ao.get("fulfillment") or {}).get("fulfilledBy") == "AMAZON"
        order = Order(
            tenant_id=tenant_id, external_order_ref=ref, status=status,
            buyer_username=((ao.get("buyer") or {}).get("buyerName") or None),
            ordered_at=ordered_at, amazon_account_id=account.id, **_ship_to(ao),
        )
        if status == "new" and not order.recipient_street1:
            notes.append(f"Bestellung {number}: keine Adresse von Amazon (Rolle für Kundendaten nicht freigegeben?).")
        db.add(order)
        db.flush()

        counts_for_stock = (
            status != "canceled" and not is_fba
            and (connected_at is None or (ordered_at is not None and ordered_at >= connected_at))
        )
        for product, quantity, price in lines:
            db.add(OrderItem(
                order_id=order.id, product_id=product.id, quantity=quantity,
                unit_price=price if price is not None else product.selling_price,
                purchase_price=product.unit_cost, shelf_location=product.shelf_location,
            ))
            if counts_for_stock:
                product.stock_quantity = max(0, product.stock_quantity - quantity)
                db.add(StockMovement(
                    tenant_id=tenant_id, product_id=product.id, quantity_change=-quantity,
                    reason="amazon_sync", reference=ref,
                ))
        created += 1

    account.last_sync_at = datetime.now(timezone.utc)
    db.commit()
    sync_fees(account, tenant_id, db, notes)
    note_refunds(account, tenant_id, db, notes)
    return {"created": created, "products_created": products_created, "skipped": skipped,
            "skipped_reasons": skipped_reasons, "notes": notes}


# -------------------------------------------------------------------- fees

def _fee_from_transactions(transactions: list[dict]) -> Decimal | None:
    total, found = Decimal("0"), False
    for tx in transactions:
        for breakdown in tx.get("breakdowns") or []:
            if breakdown.get("breakdownType") == "Expenses":
                amount = _dec((breakdown.get("breakdownAmount") or {}).get("currencyAmount"))
                if amount is not None:
                    total += abs(amount)
                    found = True
    return total if found else None


def sync_fees(account: AmazonAccount, tenant_id: UUID, db: Session, notes: list[str]) -> int:
    """Best effort; never breaks the order sync."""
    now = datetime.now(timezone.utc)
    candidates = (
        db.query(Order)
        .filter(
            Order.tenant_id == tenant_id, Order.amazon_account_id == account.id, Order.ebay_fees.is_(None),
            Order.status != "canceled",
            Order.ordered_at < now - timedelta(days=FEE_LOOKUP_MIN_AGE_DAYS),
            Order.ordered_at > now - timedelta(days=FEE_LOOKUP_MAX_AGE_DAYS),
        )
        .order_by(Order.ordered_at.desc())
        .limit(FEE_LOOKUPS_PER_SYNC)
        .all()
    )
    updated = 0
    for order in candidates:
        try:
            data = call(account, db, "GET", "/finances/2024-06-19/transactions", params={
                "relatedIdentifierName": "ORDER_ID", "relatedIdentifierValue": order.external_order_ref[len(REF_PREFIX):]})
        except AmazonRateLimited as exc:
            notes.append(f"Gebühren: {exc}")
            break
        except (ValueError, httpx.HTTPError) as exc:
            notes.append(f"Gebühren: {str(exc)[:160]}")
            break
        fee = _fee_from_transactions((data.get("payload") or data).get("transactions") or [])
        if fee is not None:
            order.ebay_fees = fee
            updated += 1
    db.commit()
    return updated


# ----------------------------------------------------------------- refunds

REFUND_LOOKBACK_DAYS = 30
REFUND_MAX_PAGES = 3


def note_refunds(account: AmazonAccount, tenant_id: UUID, db: Session, notes: list[str]) -> None:
    """Points out orders for which Amazon reports a refund and none is recorded here yet.
    The amount is NOT written to the order: Amazon's transaction total is not reliably what
    the buyer got back, and a wrong figure would silently distort the accounting."""
    since = (datetime.now(timezone.utc) - timedelta(days=REFUND_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {"postedAfter": since, "marketplaceId": account.marketplace_id}
    found: set[str] = set()
    try:
        for _ in range(REFUND_MAX_PAGES):
            data = call(account, db, "GET", "/finances/2024-06-19/transactions", params=params)
            data = data.get("payload") or data
            for tx in data.get("transactions") or []:
                if "refund" not in f"{tx.get('transactionType', '')} {tx.get('description', '')}".lower():
                    continue
                found |= {r.get("relatedIdentifierValue") for r in tx.get("relatedIdentifiers") or []
                          if r.get("relatedIdentifierName") == "ORDER_ID"}
            if not data.get("nextToken"):
                break
            params = {"postedAfter": since, "nextToken": data["nextToken"]}
    except (AmazonRateLimited, ValueError, httpx.HTTPError) as exc:
        notes.append(f"Erstattungen: {str(exc)[:160]}")
    refs = [REF_PREFIX + number for number in found if number]
    if not refs:
        return
    for order in db.query(Order).filter(Order.tenant_id == tenant_id, Order.external_order_ref.in_(refs),
                                        Order.refund_amount.is_(None)).all():
        notes.append(f"Bestellung {order.external_order_ref[len(REF_PREFIX):]}: Amazon meldet eine Erstattung – "
                     "bitte den Betrag unter „Rückgabe / Erstattung“ eintragen.")


# ---------------------------------------------------------------- shipping

def _carrier(carrier: str) -> dict:
    names = {"DHL": "DHL", "DPD": "DPD", "HERMES": "Hermes", "GLS": "GLS", "UPS": "UPS", "DEUTSCHE_POST": "Deutsche Post"}
    if carrier == "DHL":
        return {"carrierCode": "DHL"}
    return {"carrierCode": "Other", "carrierName": names.get(carrier, carrier)}


def mark_order_shipped(account: AmazonAccount, order: Order, tracking_number: str, carrier: str, db: Session) -> None:
    """Confirms the shipment (with tracking) at Amazon, then marks the order shipped."""
    tracking_number = "".join(tracking_number.split()).replace("-", "")
    if not tracking_number:
        raise ValueError("Sendungsnummer fehlt.")
    ref = order.external_order_ref or ""
    if not ref.startswith(REF_PREFIX):
        raise ValueError("Diese Bestellung stammt nicht von Amazon.")
    amazon_id = ref[len(REF_PREFIX):]

    data = call(account, db, "GET", f"/orders/2026-01-01/orders/{amazon_id}")
    items = [
        {"orderItemId": i["orderItemId"], "quantity": int(i.get("quantityOrdered") or 1)}
        for i in (data.get("order") or data).get("orderItems") or [] if i.get("orderItemId")
    ]
    if not items:
        raise ValueError("Amazon lieferte keine Positionen für diese Bestellung.")

    call(account, db, "POST", f"/orders/v0/orders/{amazon_id}/shipmentConfirmation", json={
        "marketplaceId": account.marketplace_id,
        "packageDetail": {
            "packageReferenceId": "1", "trackingNumber": tracking_number,
            "shipDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "orderItems": items, **_carrier(carrier),
        },
    })
    order.tracking_number = tracking_number[:50]
    order.status = "shipped"
    db.commit()
