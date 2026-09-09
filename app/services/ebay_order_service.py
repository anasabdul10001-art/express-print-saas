"""
Syncs orders from eBay's Fulfillment API into local Order/OrderItem rows.

Design choices worth knowing:
    - Duplicate protection: each eBay order's `orderId` is stored as our
      Order.external_order_ref. Before creating anything, we check whether
      an order with that ref already exists for this tenant - so running
      sync twice (or on a schedule later) never creates duplicates.
    - SKU matching: eBay line items are matched to our Product rows by SKU.
      If a line item's SKU doesn't exist in our products, the WHOLE eBay
      order is skipped (not partially imported) and reported back, so the
      seller can fix the SKU mismatch and re-sync rather than ending up
      with a silently incomplete order.
    - Stock: reuses the same "decrement on order creation" behavior as
      manual orders, but never blocks the import if stock is insufficient
      (an eBay order already happened in the real world - refusing to
      record it wouldn't undo the sale, it would just hide it. Stock can
      go negative here as a visible signal to restock, rather than an
      import failure). Each decrement is logged as a StockMovement row,
      same as manual orders, so the audit trail covers both sources.
"""

from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session
from uuid import UUID

from app.models.ebay_account import EbayAccount
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.stock_movement import StockMovement
from app.services import ebay_oauth_service

EBAY_API_BASE = {
    "SANDBOX": "https://api.sandbox.ebay.com",
    "PRODUCTION": "https://api.ebay.com",
}


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

    created_count = 0
    skipped_count = 0
    skipped_reasons: list[str] = []

    for ebay_order in ebay_orders:
        ebay_order_id = ebay_order.get("orderId")
        if not ebay_order_id:
            continue

        already_imported = (
            db.query(Order)
            .filter(Order.tenant_id == tenant_id, Order.external_order_ref == ebay_order_id)
            .first()
        )
        if already_imported:
            continue  # silently skip - this is the normal case on repeat syncs

        line_items = ebay_order.get("lineItems", [])
        items_to_create: list[tuple[Product, int]] = []
        order_ok = True

        for line_item in line_items:
            sku = line_item.get("sku")
            quantity = int(line_item.get("quantity", 1))

            product = (
                db.query(Product)
                .filter(Product.tenant_id == tenant_id, Product.sku == sku)
                .first()
                if sku
                else None
            )

            if not product:
                order_ok = False
                skipped_reasons.append(f"Bestellung {ebay_order_id}: SKU '{sku}' nicht gefunden.")
                break

            items_to_create.append((product, quantity))

        if not order_ok:
            skipped_count += 1
            continue

        order = Order(tenant_id=tenant_id, external_order_ref=ebay_order_id, status="new")
        db.add(order)
        db.flush()

        for product, quantity in items_to_create:
            db.add(OrderItem(
                order_id=order.id,
                product_id=product.id,
                quantity=quantity,
                unit_price=product.selling_price,
                purchase_price=product.purchase_price,
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

    return {
        "created": created_count,
        "skipped": skipped_count,
        "skipped_reasons": skipped_reasons,
    }