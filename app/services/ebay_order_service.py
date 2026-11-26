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
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy.orm import Session
from uuid import UUID

from app.models.cash_event import CashEvent
from app.models.ebay_account import EbayAccount
from app.models.expense import Expense
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.services import stock_listing_service
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


def _parse_ebay_date(value):
    """eBay sends ISO 8601 like 2026-11-20T14:03:11.000Z; None if missing/unreadable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _refund_total(ebay_order: dict):
    """Total paid back to the buyer according to eBay's payment summary (None if no refund)."""
    refunds = (ebay_order.get("paymentSummary") or {}).get("refunds") or []
    total = Decimal("0")
    for refund in refunds:
        try:
            total += Decimal(str((refund.get("amount") or {}).get("value")))
        except (InvalidOperation, TypeError, ValueError):
            continue
    return total.quantize(Decimal("0.01")) if total > 0 else None


def _dec(value):
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _apply_amounts(order: Order, ebay_order: dict) -> None:
    """Copies eBay's own money figures onto the order (buyer total, payout, payment status, refund)."""
    summary = ebay_order.get("paymentSummary") or {}
    pricing = ebay_order.get("pricingSummary") or {}
    total = _dec((pricing.get("total") or {}).get("value"))
    if total is not None:
        order.ebay_buyer_total = total
    payout = _dec((summary.get("totalDueSeller") or {}).get("value"))
    if payout is not None:
        order.ebay_payout = payout
    status = ebay_order.get("orderPaymentStatus")
    if status:
        order.ebay_payment_status = status
    refunded = _refund_total(ebay_order)
    if status == "FULLY_REFUNDED" and total is not None:
        refunded = total  # the buyer got everything back (eBay's own refund figure is net of returned fees)
    if refunded is not None and order.refund_amount != refunded:
        order.refund_amount = refunded


def _apply_fulfillment(order: Order, ebay_order: dict) -> None:
    """An order shipped outside this app (e.g. directly at eBay) is shipped here too."""
    if ebay_order.get("orderFulfillmentStatus") == "FULFILLED" and order.status != "canceled":
        order.status = "shipped"


ORDER_PAGE_SIZE = 200  # the most eBay returns per request
ORDER_MAX_PAGES = 25


def _fetch_orders(base_url: str, access_token: str) -> list[dict]:
    """Every order eBay lists, page by page, so a new sale is never cut off."""
    orders: list[dict] = []
    for page in range(ORDER_MAX_PAGES):
        resp = httpx.get(
            f"{base_url}/sell/fulfillment/v1/order",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            params={"limit": ORDER_PAGE_SIZE, "offset": page * ORDER_PAGE_SIZE},
            timeout=20.0,
        )
        if resp.status_code != 200:
            raise ValueError(f"ebay_fetch_orders_failed: {resp.text}")
        batch = resp.json().get("orders", [])
        orders += batch
        if len(batch) < ORDER_PAGE_SIZE:
            break
    return orders


def sync_orders(account: EbayAccount, tenant_id: UUID, db: Session) -> dict:
    access_token = ebay_oauth_service.get_valid_access_token(account, db)
    base_url = EBAY_API_BASE[account.environment]

    ebay_orders = _fetch_orders(base_url, access_token)

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
            if already_imported.ordered_at is None:
                already_imported.ordered_at = _parse_ebay_date(ebay_order.get("creationDate"))
            # A refund can arrive long after the sale; eBay is the source of truth.
            _apply_amounts(already_imported, ebay_order)
            _apply_fulfillment(already_imported, ebay_order)
            lines = ebay_order.get("lineItems", [])
            if len(lines) == len(already_imported.items):
                for item, line in zip(already_imported.items, lines):
                    price = _unit_price(line, int(line.get("quantity", 1)))
                    if price is not None and item.unit_price != price:
                        item.unit_price = price
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

            if product:
                stock_listing_service.now_listed(db, product)

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
            ordered_at=_parse_ebay_date(ebay_order.get("creationDate")),
            ebay_account_id=account.id,
            **_extract_ship_to(ebay_order),
        )
        _apply_amounts(order, ebay_order)
        _apply_fulfillment(order, ebay_order)
        db.add(order)
        db.flush()

        for (product, quantity), line_item in zip(items_to_create, line_items):
            db.add(OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=quantity,
                unit_price=_unit_price(line_item, quantity) or product.selling_price,
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


def _fetch_transactions(account, db, access_token, kind, notes):
    """All Finances transactions of one type (paged, each request signed). None on failure."""
    url = f"{FINANCES_API_BASE[account.environment]}/sell/finances/v1/transaction"
    out: list[dict] = []
    for page in range(10):
        try:
            headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
            headers.update(ebay_signing_service.signed_headers(account, db, "GET", url))
            resp = httpx.get(
                url, headers=headers, timeout=20.0,
                params={"filter": f"transactionType:{{{kind}}}", "limit": 200, "offset": page * 200},
            )
        except (ebay_signing_service.SigningError, httpx.HTTPError, ValueError) as exc:
            if notes is not None:
                notes.append(f"Finanzen {kind}: {exc.__class__.__name__}: {exc}")
            return None
        if resp.status_code != 200:
            if notes is not None:
                notes.append(f"Finanzen {kind}: eBay antwortet {resp.status_code}: {resp.text[:200]}")
            return None
        batch = resp.json().get("transactions", [])
        out += batch
        if len(batch) < 200:
            break
    return out


# Monthly eBay Store subscription (Finances API feeType); kept apart from per-listing fees.
STORE_SUBSCRIPTION_CATEGORY = "eBay-Shop-Abo"


def _tx_amount(tx):
    """Signed amount of a transaction: DEBIT (money out) positive, CREDIT negative."""
    value = _dec((tx.get("amount") or {}).get("value"))
    if value is None:
        return None
    return -value if tx.get("bookingEntry") == "CREDIT" else value


def _record_events(db: Session, tenant_id: UUID, found: dict) -> None:
    """Saves the day of each order transaction for the cash-basis report. `found` maps
    (order, kind, eBay transaction id) to (amount, day); known ones are updated, never doubled."""
    if not found:
        return
    known = {(e.order_id, e.kind, e.external_ref): e for e in db.query(CashEvent).filter(
        CashEvent.tenant_id == tenant_id, CashEvent.order_id.in_({key[0] for key in found}))}
    for (order_id, kind, ref), (amount, day) in found.items():
        event = known.get((order_id, kind, ref))
        if event is None:
            db.add(CashEvent(tenant_id=tenant_id, order_id=order_id, kind=kind, external_ref=ref,
                             amount=amount, booked_on=day))
        elif event.amount != amount or event.booked_on != day:
            event.amount, event.booked_on = amount, day


def _collect(found: dict, order, kind: str, tx: dict, amount) -> None:
    """Adds one transaction to `found`; without an id or a date it is left out."""
    ref, when = tx.get("transactionId"), _parse_ebay_date(tx.get("transactionDate"))
    if not ref or when is None or amount is None:
        return
    key = (order.id, kind, str(ref)[:100])
    before = found.get(key, (Decimal("0"), None))[0]
    found[key] = (before + amount, when.astimezone(ZoneInfo("Europe/Berlin")).date())


def sync_fees(
    account: EbayAccount, tenant_id: UUID, db: Session, access_token: str, notes: list[str] | None = None
) -> int:
    """
    Stores eBay's real money movements (Finances API): the fee per sale, refunds with the fees eBay
    gave back, the shipping label cost per order, and fees not tied to an order (insertion fees ...)
    as expenses. Best effort: a failing call is reported in `notes` and never breaks the order sync.
    """
    updated = 0
    found: dict = {}  # dated money movements, saved at the end

    def order_for(ref):
        return db.query(Order).filter(Order.tenant_id == tenant_id, Order.external_order_ref == ref).first()

    sales = _fetch_transactions(account, db, access_token, "SALE", notes)
    if sales is not None:
        if not sales and notes is not None:
            notes.append("Gebühren: eBay lieferte keine Verkaufs-Transaktionen.")
        for tx in sales:
            fee = _dec((tx.get("totalFeeAmount") or {}).get("value"))
            order = order_for(tx.get("orderId")) if tx.get("orderId") else None
            if order and fee:
                _collect(found, order, "sale_fee", tx, fee)
            if order and fee is not None and order.ebay_fees != fee:
                order.ebay_fees = fee
                updated += 1

    refunds = _fetch_transactions(account, db, access_token, "REFUND", notes)
    if refunds is not None:
        per_order: dict[str, list[Decimal]] = {}
        for tx in refunds:
            amount, credit = _tx_amount(tx), _dec((tx.get("totalFeeAmount") or {}).get("value")) or Decimal("0")
            if tx.get("orderId") and amount is not None:
                entry = per_order.setdefault(tx["orderId"], [Decimal("0"), Decimal("0")])
                entry[0] += amount + credit  # what the buyer got back = eBay's net refund + the fee it returned
                entry[1] += credit
                order = order_for(tx["orderId"])
                if order:
                    _collect(found, order, "refund", tx, amount + credit)
                    if credit:
                        _collect(found, order, "fee_credit", tx, credit)
        for ref, (refunded, credit) in per_order.items():
            order = order_for(ref)
            if order:
                if order.refund_amount != refunded.quantize(Decimal("0.01")):
                    order.refund_amount = refunded.quantize(Decimal("0.01"))
                    updated += 1
                order.ebay_fee_credit = credit.quantize(Decimal("0.01"))

    labels = _fetch_transactions(account, db, access_token, "SHIPPING_LABEL", notes)
    if labels is not None:
        per_order_labels: dict[str, Decimal] = {}
        for tx in labels:
            amount = _tx_amount(tx)
            if tx.get("orderId") and amount is not None:
                per_order_labels[tx["orderId"]] = per_order_labels.get(tx["orderId"], Decimal("0")) + amount
                order = order_for(tx["orderId"])
                if order:
                    _collect(found, order, "label", tx, amount)
        for ref, cost in per_order_labels.items():
            order = order_for(ref)
            if order and order.shipping_label_cost != cost:
                order.shipping_label_cost = cost
                updated += 1

    charges = _fetch_transactions(account, db, access_token, "NON_SALE_CHARGE", notes)
    if charges is not None:
        known = {r for (r,) in db.query(Expense.external_ref).filter(
            Expense.tenant_id == tenant_id, Expense.external_ref.isnot(None))}
        for tx in charges:
            ref, amount = tx.get("transactionId"), _tx_amount(tx)
            if not ref or amount is None or ref in known:
                continue
            when = _parse_ebay_date(tx.get("transactionDate")) or datetime.now(timezone.utc)
            category = STORE_SUBSCRIPTION_CATEGORY if tx.get("feeType") == "EBAY_STORE_SUBSCRIPTION_FEE" else "eBay-Gebühren (Angebote)"
            db.add(Expense(
                tenant_id=tenant_id, amount=amount, category=category,
                description=(tx.get("transactionMemo") or tx.get("feeType") or "eBay-Gebühr")[:255],
                expense_date=when.astimezone(ZoneInfo("Europe/Berlin")).date(), external_ref=ref[:100],
            ))
            known.add(ref)
            updated += 1

    _record_events(db, tenant_id, found)
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
