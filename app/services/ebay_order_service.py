"""
Syncs orders from eBay's Fulfillment API into local Order/OrderItem rows.

Design choices worth knowing:
    - Duplicate protection: each eBay order's `orderId` is stored as our
      Order.external_order_ref. Before creating anything, we check whether
      an order with that ref already exists for this tenant - so running
      sync twice (or on a schedule later) never creates duplicates.
    - SKU matching: eBay line items are matched to our Product rows by SKU
      (or by the eBay item number when the listing has no custom label).
      A line item whose product doesn't exist yet creates that product from
      the order line (title, unit price, stock 0, no purchase price), so a
      sale is never hidden just because the seller hasn't imported products.
      Only a line with neither SKU nor item number is skipped (the WHOLE
      order, never partially imported) and reported back.
    - Stock: reuses the same "decrement on order creation" behavior as
      manual orders, but never blocks the import if stock is insufficient
      (an eBay order already happened in the real world - refusing to
      record it wouldn't undo the sale, it would just hide it. Stock can
      go negative here as a visible signal to restock, rather than an
      import failure). Each decrement is logged as a StockMovement row,
      same as manual orders, so the audit trail covers both sources.
    - Recipient address: read from the order's
      fulfillmentStartInstructions[].shippingStep.shipTo (eBay's Fulfillment
      API - see the Order/ExtendedContact type reference), needed later to
      actually create a DHL shipment label. Missing entirely for orders
      older than ~90 days (eBay stops returning buyer PII by then) or if
      eBay simply didn't include a shipping instruction - in both cases the
      order still imports, just without an address, same as a manually
      created order.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy.orm import Session
from uuid import UUID

from app.models.ebay_account import EbayAccount
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.stock_movement import StockMovement
from app.services import ebay_oauth_service, ebay_signing_service
from app.services.ebay_deletion_service import deleted_order_ref

EBAY_API_BASE = {
    "SANDBOX": "https://api.sandbox.ebay.com",
    "PRODUCTION": "https://api.ebay.com",
}


def _unit_price(line_item: dict, quantity: int):
    """Price per piece from the line's total cost; None if eBay did not send a usable figure."""
    try:
        total = Decimal(str((line_item.get("lineItemCost") or {}).get("value")))
        return (total / max(quantity, 1)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _extract_ship_to(ebay_order: dict) -> dict:
    """
    Pulls the recipient address out of the first SHIP_TO fulfillment
    instruction, if there is one. Every field is optional in eBay's
    response, so this always returns plain Nones rather than raising -
    a partially-missing address is still worth storing (e.g. no phone
    number shouldn't discard the street address too).
    """
    for instruction in ebay_order.get("fulfillmentStartInstructions") or []:
        ship_to = (instruction.get("shippingStep") or {}).get("shipTo")
        if not ship_to:
            continue
        contact_address = ship_to.get("contactAddress") or {}
        primary_phone = ship_to.get("primaryPhone") or {}
        return {
            "recipient_name": ship_to.get("fullName"),
            "recipient_street1": contact_address.get("addressLine1"),
            "recipient_street2": contact_address.get("addressLine2"),
            "recipient_city": contact_address.get("city"),
            "recipient_state": contact_address.get("stateOrProvince"),
            "recipient_zip": contact_address.get("postalCode"),
            "recipient_country_code": contact_address.get("countryCode"),
            "recipient_phone": primary_phone.get("phoneNumber"),
        }
    return {}


def sync_orders(account: EbayAccount, tenant_id: UUID, db: Session) -> dict:
    access_token = ebay_oauth_service.get_valid_access_token(account, db)
    base_url = EBAY_API_BASE[account.environment]

    resp = httpx.get(
        f"{base_url}/sell/fulfillment/v1/order",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        params={"limit": 50},
        timeout=20.0,
    )
    if resp.status_code != 200:
        raise ValueError(f"ebay_fetch_orders_failed: {resp.text}")

    ebay_orders = resp.json().get("orders", [])

    # Accounts connected before the identity scope existed have no stored
    # username; every order names its seller, so fill it in from there.
    if not account.ebay_user_id and ebay_orders:
        account.ebay_user_id = ebay_orders[0].get("sellerId")

    created_count = 0
    products_created = 0
    skipped_count = 0
    skipped_reasons: list[str] = []

    for ebay_order in ebay_orders:
        ebay_order_id = ebay_order.get("orderId")
        if not ebay_order_id:
            continue
        buyer_username = (ebay_order.get("buyer") or {}).get("username")

        already_imported = (
            db.query(Order)
            .filter(
                Order.tenant_id == tenant_id,
                # The second form means this buyer's data was deleted at
                # eBay's request, so it must not be imported again.
                Order.external_order_ref.in_([ebay_order_id, deleted_order_ref(ebay_order_id)]),
            )
            .first()
        )
        if already_imported:
            # Orders imported before buyer_username existed can't be found by
            # a deletion notification until they get it.
            if not already_imported.buyer_username and already_imported.external_order_ref == ebay_order_id:
                already_imported.buyer_username = buyer_username
            continue  # silently skip - this is the normal case on repeat syncs

        line_items = ebay_order.get("lineItems", [])
        items_to_create: list[tuple[Product, int]] = []
        order_ok = True

        for line_item in line_items:
            # Listings without a custom label have no "sku" in the order; the CSV
            # import then uses the eBay item number as SKU, so match on that.
            sku = line_item.get("sku") or line_item.get("legacyItemId")
            quantity = int(line_item.get("quantity", 1))

            product = (
                db.query(Product)
                .filter(Product.tenant_id == tenant_id, Product.sku == sku)
                .first()
                if sku
                else None
            )

            if not product and sku:
                # An unknown product must not hide a real sale: create it from
                # the order line (no purchase price yet, so no profit figure
                # until the seller fills it in) and import the order normally.
                product = Product(
                    tenant_id=tenant_id,
                    sku=sku,
                    title=(line_item.get("title") or sku)[:255],
                    selling_price=_unit_price(line_item, quantity),
                    stock_quantity=0,
                )
                db.add(product)
                db.flush()
                products_created += 1

            if not product:
                order_ok = False
                skipped_reasons.append(
                    f"Bestellung {ebay_order_id}: Position ohne SKU und ohne eBay-Artikelnummer."
                )
                break

            items_to_create.append((product, quantity))

        if not order_ok:
            skipped_count += 1
            continue

        order = Order(
            tenant_id=tenant_id,
            external_order_ref=ebay_order_id,
            status="new",
            buyer_username=buyer_username,
            ebay_account_id=account.id,
            **_extract_ship_to(ebay_order),
        )
        db.add(order)
        db.flush()

        for product, quantity in items_to_create:
            db.add(OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=quantity,
                unit_price=product.selling_price,
                purchase_price=product.unit_cost,
                shelf_location=product.shelf_location,
            ))
            # Best-effort decrement; never blocks the import if stock is
            # insufficient - see module docstring.
            product.stock_quantity = max(0, product.stock_quantity - quantity)

            db.add(StockMovement(
                tenant_id=tenant_id,
                product_id=product.id,
                quantity_change=-quantity,
                reason="ebay_sync",
                reference=ebay_order_id,
            ))

        created_count += 1

    account.last_sync_at = datetime.now(timezone.utc)
    db.commit()

    fee_notes: list[str] = []
    fees_updated = sync_fees(account, tenant_id, db, access_token, fee_notes)

    return {
        "fees_updated": fees_updated,
        "fee_notes": fee_notes,
        "created": created_count,
        "products_created": products_created,
        "skipped": skipped_count,
        "skipped_reasons": skipped_reasons,
    }

FINANCES_API_BASE = {
    "SANDBOX": "https://apiz.sandbox.ebay.com",
    "PRODUCTION": "https://apiz.ebay.com",
}


def sync_fees(
    account: EbayAccount, tenant_id: UUID, db: Session, access_token: str, notes: list[str] | None = None
) -> int:
    """
    Stores eBay's real fee per sale on the matching orders (Finances API).
    Best effort: accounts connected before the finances scope existed get a
    401/403 here and simply keep showing profit without fees until they
    reconnect; this never breaks the order sync.
    """
    url = f"{FINANCES_API_BASE[account.environment]}/sell/finances/v1/transaction"
    try:
        headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        headers.update(ebay_signing_service.signed_headers(account, db, "GET", url))
    except (ebay_signing_service.SigningError, httpx.HTTPError, ValueError) as exc:
        if notes is not None:
            notes.append(f"Gebühren: Signatur nicht möglich ({exc})")
        return 0
    try:
        resp = httpx.get(
            url,
            headers=headers,
            params={"filter": "transactionType:{SALE}", "limit": 200},
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        if notes is not None:
            notes.append(f"Gebühren: Netzwerkfehler ({exc.__class__.__name__})")
        return 0
    if resp.status_code != 200:
        if notes is not None:
            notes.append(f"Gebühren: eBay antwortet {resp.status_code}: {resp.text[:200]}")
        return 0

    transactions = resp.json().get("transactions", [])
    if not transactions and notes is not None:
        notes.append("Gebühren: eBay lieferte keine Verkaufs-Transaktionen.")
    updated = 0
    for tx in transactions:
        order_ref = tx.get("orderId")
        try:
            fee = Decimal(str((tx.get("totalFeeAmount") or {}).get("value"))).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if not order_ref:
            continue
        order = db.query(Order).filter(Order.tenant_id == tenant_id, Order.external_order_ref == order_ref).first()
        if order and order.ebay_fees != fee:
            order.ebay_fees = fee
            updated += 1
    db.commit()
    return updated


SHIPPING_CARRIERS = ("DHL", "DPD", "HERMES", "GLS", "UPS", "DEUTSCHE_POST")


def mark_order_shipped(
    account: EbayAccount, order: Order, tracking_number: str, carrier: str, db: Session
) -> None:
    """
    Tells eBay the order has shipped and hands it the tracking number, so the
    buyer sees it in eBay. Then marks the local order as shipped.
    eBay needs the order's line item ids, so the order is fetched first.
    Raises ValueError with eBay's message on any failure.
    """
    tracking_number = "".join(tracking_number.split()).replace("-", "")
    if not tracking_number:
        raise ValueError("Sendungsnummer fehlt.")
    if not order.external_order_ref or order.external_order_ref.startswith("deleted:"):
        raise ValueError("Diese Bestellung stammt nicht von eBay.")

    access_token = ebay_oauth_service.get_valid_access_token(account, db)
    base = f"{EBAY_API_BASE[account.environment]}/sell/fulfillment/v1/order/{order.external_order_ref}"
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}

    got = httpx.get(base, headers=headers, timeout=20.0)
    if got.status_code != 200:
        raise ValueError(f"ebay_fetch_order_failed: {got.text}")
    line_items = [
        {"lineItemId": li["lineItemId"], "quantity": int(li.get("quantity", 1))}
        for li in got.json().get("lineItems", [])
        if li.get("lineItemId")
    ]
    if not line_items:
        raise ValueError("eBay lieferte keine Positionen fuer diese Bestellung.")

    posted = httpx.post(
        f"{base}/shipping_fulfillment",
        headers=headers,
        json={
            "lineItems": line_items,
            "shippedDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "shippingCarrierCode": carrier,
            "trackingNumber": tracking_number,
        },
        timeout=20.0,
    )
    if posted.status_code not in (200, 201):
        raise ValueError(f"ebay_shipping_fulfillment_failed: {posted.text}")

    order.tracking_number = tracking_number[:50]
    order.status = "shipped"
    db.commit()
