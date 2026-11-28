"""
Shopify Admin GraphQL: import products, sync orders (with refunds + payment
fees) and report tracking - the same jobs the eBay services do.

Notes worth knowing:
    - Orders and products become the same Order / Product rows as eBay ones
      (Order.shopify_account_id says where an order came from), so profit,
      accounting and refunds work unchanged. external_order_ref is
      "shopify:<numeric id>", which can never collide with an eBay order id.
    - Customer name/address are "protected customer data": until the app is
      approved for it Shopify answers with an access error. The sync then
      retries without them and still imports the sale (no address = no label).
    - Stock: Shopify already subtracted a sale from its own inventory, so
      importing products takes the CURRENT inventory as the starting stock and
      only orders placed after the store was connected decrement it afterwards
      (older orders are history). Like eBay, stock never blocks an import.
    - Fees: Shopify Payments fees come from OrderTransaction.fees. Stores on
      other gateways have none, so the fee stays empty (never guessed). The
      value is stored in the shared Order.ebay_fees column (= marketplace fees).
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.services import stock_listing_service
from app.models.shopify_account import ShopifyAccount
from app.models.stock_movement import StockMovement
from app.services import shopify_oauth_service

REF_PREFIX = "shopify:"


class ShopifyAccessDenied(ValueError):
    """Shopify refused a field (e.g. protected customer data not approved)."""


def gid_number(gid: str | None) -> str | None:
    """'gid://shopify/Order/12345' -> '12345'."""
    return gid.rsplit("/", 1)[-1] if gid else None


def graphql(account: ShopifyAccount, query: str, variables: dict | None = None) -> dict:
    token = shopify_oauth_service.get_access_token(account)
    resp = httpx.post(
        f"https://{account.shop_domain}/admin/api/{settings.shopify_api_version}/graphql.json",
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
        timeout=30.0,
    )
    if resp.status_code in (401, 403):
        raise ValueError(f"shopify_unauthorized ({resp.status_code}): reconnect the store")
    if resp.status_code != 200:
        raise ValueError(f"shopify_request_failed ({resp.status_code}): {resp.text[:300]}")
    body = resp.json()
    errors = body.get("errors")
    if errors:
        text = str(errors)
        if "ACCESS_DENIED" in text or "not approved" in text.lower() or "protected customer" in text.lower():
            raise ShopifyAccessDenied(text[:300])
        raise ValueError(f"shopify_graphql_error: {text[:300]}")
    return body.get("data") or {}


def _money(node) -> Decimal | None:
    try:
        return Decimal(str((node or {}).get("amount"))).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _shop_money(set_node) -> Decimal | None:
    return _money((set_node or {}).get("shopMoney"))


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------- products

PRODUCTS_QUERY = """
query($cursor: String) {
  shop { name }
  products(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      title status featuredImage { url }
      variants(first: 100) {
        nodes { id sku title price inventoryQuantity image { url } }
      }
    }
  }
}
"""


def fetch_variants(account: ShopifyAccount, max_pages: int = 20) -> tuple[list[dict], str | None]:
    """Every variant of the store as flat dicts (one NettoDesk product each)."""
    variants, cursor, shop_name = [], None, None
    for _ in range(max_pages):
        data = graphql(account, PRODUCTS_QUERY, {"cursor": cursor})
        shop_name = (data.get("shop") or {}).get("name") or shop_name
        page = data.get("products") or {}
        for product in page.get("nodes") or []:
            if product.get("status") == "ARCHIVED":
                continue
            for variant in (product.get("variants") or {}).get("nodes") or []:
                vtitle = variant.get("title")
                title = product.get("title") or ""
                if vtitle and vtitle != "Default Title":
                    title = f"{title} - {vtitle}"
                variants.append({
                    "sku": variant.get("sku") or gid_number(variant.get("id")),
                    "variant_id": gid_number(variant.get("id")),
                    "title": title[:500],
                    "price": _money({"amount": variant.get("price")}),
                    "stock": variant.get("inventoryQuantity"),
                    "image_url": (variant.get("image") or {}).get("url") or (product.get("featuredImage") or {}).get("url"),
                })
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        cursor = info.get("endCursor")
    return variants, shop_name


def import_products(account: ShopifyAccount, tenant_id: UUID, db: Session) -> dict:
    """Creates the missing products (SKU = variant SKU, else the variant id).
    Existing products are left untouched."""
    variants, shop_name = fetch_variants(account)
    if shop_name and not account.shop_name:
        account.shop_name = shop_name
    known = {sku for (sku,) in db.query(Product.sku).filter(Product.tenant_id == tenant_id).all()}
    stock_listing_service.seen_in_shop(db, tenant_id, [
        (v["sku"], v["image_url"], v["price"]) for v in variants if v["sku"]])
    created = 0
    for v in variants:
        if not v["sku"] or v["sku"] in known:
            continue
        known.add(v["sku"])
        stock = v["stock"] if isinstance(v["stock"], int) else 0
        db.add(Product(
            tenant_id=tenant_id, sku=v["sku"], title=v["title"] or v["sku"],
            selling_price=v["price"], stock_quantity=max(0, stock), image_url=v["image_url"],
        ))
        created += 1
    db.commit()
    return {"listings_found": len(variants), "products_created": created, "already_known": len(variants) - created}


# ------------------------------------------------------------------ orders

_ORDER_FIELDS = """
  id name createdAt cancelledAt displayFulfillmentStatus
  totalRefundedSet { shopMoney { amount } }
  transactions(first: 20) { fees { amount { amount } } }
  lineItems(first: 100) {
    nodes { title quantity sku variant { id } originalUnitPriceSet { shopMoney { amount } } discountedUnitPriceSet { shopMoney { amount } } }
  }
"""
_PII_FIELDS = """
  customer { displayName }
  shippingAddress { name address1 address2 city province zip countryCodeV2 phone }
"""


def _orders_query(with_pii: bool) -> str:
    return "query { orders(first: 50, sortKey: CREATED_AT, reverse: true) { nodes { %s %s } } }" % (
        _ORDER_FIELDS, _PII_FIELDS if with_pii else "")


def _fetch_orders(account: ShopifyAccount, notes: list[str]) -> list[dict]:
    try:
        data = graphql(account, _orders_query(True))
    except ShopifyAccessDenied:
        notes.append("Kundendaten (Name/Adresse) sind für diese App noch nicht freigegeben - Bestellungen ohne Adresse importiert.")
        data = graphql(account, _orders_query(False))
    return (data.get("orders") or {}).get("nodes") or []


def _ship_to(order: dict) -> dict:
    a = order.get("shippingAddress") or {}
    if not a:
        return {}
    return {
        "recipient_name": a.get("name"), "recipient_street1": a.get("address1"),
        "recipient_street2": a.get("address2"), "recipient_city": a.get("city"),
        "recipient_state": a.get("province"), "recipient_zip": a.get("zip"),
        "recipient_country_code": a.get("countryCodeV2"), "recipient_phone": a.get("phone"),
    }


def _fees(order: dict) -> Decimal | None:
    total, found = Decimal("0"), False
    for tx in order.get("transactions") or []:
        for fee in tx.get("fees") or []:
            amount = _money(fee.get("amount"))
            if amount is not None:
                total += amount
                found = True
    return total if found else None


def _refund(order: dict) -> Decimal | None:
    refunded = _shop_money(order.get("totalRefundedSet"))
    return refunded if refunded and refunded > 0 else None


def _unit_price(line: dict) -> Decimal | None:
    return _shop_money(line.get("discountedUnitPriceSet")) or _shop_money(line.get("originalUnitPriceSet"))


def sync_orders(account: ShopifyAccount, tenant_id: UUID, db: Session) -> dict:
    notes: list[str] = []
    shopify_orders = _fetch_orders(account, notes)
    created = products_created = skipped = 0
    skipped_reasons: list[str] = []
    connected_at = account.connected_at

    for so in shopify_orders:
        number = gid_number(so.get("id"))
        if not number:
            continue
        ref = REF_PREFIX + number

        existing = db.query(Order).filter(Order.tenant_id == tenant_id, Order.external_order_ref == ref).first()
        if existing:
            # Refunds and fees can appear long after the sale; Shopify is the source of truth.
            refunded = _refund(so)
            if refunded is not None and existing.refund_amount != refunded:
                existing.refund_amount = refunded
            fee = _fees(so)
            if fee is not None and existing.ebay_fees != fee:
                existing.ebay_fees = fee
            continue

        lines, ok = [], True
        for line in (so.get("lineItems") or {}).get("nodes") or []:
            quantity = int(line.get("quantity") or 1)
            sku = line.get("sku") or gid_number((line.get("variant") or {}).get("id"))
            if not sku:
                ok = False
                skipped_reasons.append(f"Bestellung {so.get('name') or number}: Position ohne SKU und ohne Variante (gelöschtes Produkt?).")
                break
            product = db.query(Product).filter(Product.tenant_id == tenant_id, Product.sku == sku).first()
            if not product:
                product = Product(
                    tenant_id=tenant_id, sku=sku, title=(line.get("title") or sku)[:255],
                    selling_price=_unit_price(line), stock_quantity=0,
                )
                db.add(product)
                db.flush()
                products_created += 1
            lines.append((product, quantity, _unit_price(line)))
        if not ok:
            skipped += 1
            continue

        ordered_at = _parse_date(so.get("createdAt"))
        cancelled = bool(so.get("cancelledAt"))
        fulfilled = so.get("displayFulfillmentStatus") == "FULFILLED"
        order = Order(
            tenant_id=tenant_id, external_order_ref=ref,
            status="canceled" if cancelled else ("shipped" if fulfilled else "new"),
            buyer_username=((so.get("customer") or {}).get("displayName") or None),
            ordered_at=ordered_at, refund_amount=_refund(so), ebay_fees=_fees(so),
            shopify_account_id=account.id, **_ship_to(so),
        )
        db.add(order)
        db.flush()

        is_new_sale = not cancelled and (connected_at is None or (ordered_at is not None and ordered_at >= connected_at))
        for product, quantity, price in lines:
            db.add(OrderItem(
                order_id=order.id, product_id=product.id, quantity=quantity,
                unit_price=price if price is not None else product.selling_price,
                purchase_price=product.unit_cost, shelf_location=product.shelf_location,
            ))
            if is_new_sale:
                product.stock_quantity = max(0, product.stock_quantity - quantity)
                db.add(StockMovement(
                    tenant_id=tenant_id, product_id=product.id, quantity_change=-quantity,
                    reason="shopify_sync", reference=ref,
                ))
        created += 1

    account.last_sync_at = datetime.now(timezone.utc)
    db.commit()
    return {"created": created, "products_created": products_created, "skipped": skipped,
            "skipped_reasons": skipped_reasons, "notes": notes}


# ---------------------------------------------------------------- shipping

CARRIER_NAMES = {"DHL": "DHL", "DPD": "DPD", "HERMES": "Hermes", "GLS": "GLS", "UPS": "UPS", "DEUTSCHE_POST": "Deutsche Post"}

FULFILLMENT_ORDERS_QUERY = """
query($id: ID!) { order(id: $id) { fulfillmentOrders(first: 10) { nodes { id status } } } }
"""
FULFILLMENT_CREATE = """
mutation($f: FulfillmentInput!) {
  fulfillmentCreate(fulfillment: $f) { fulfillment { id status } userErrors { field message } }
}
"""


def mark_order_shipped(account: ShopifyAccount, order: Order, tracking_number: str, carrier: str, db: Session) -> None:
    """Creates a fulfillment with tracking at Shopify (Shopify notifies the
    buyer), then marks the local order shipped. ValueError with Shopify's
    message on any failure."""
    tracking_number = "".join(tracking_number.split()).replace("-", "")
    if not tracking_number:
        raise ValueError("Sendungsnummer fehlt.")
    ref = order.external_order_ref or ""
    if not ref.startswith(REF_PREFIX):
        raise ValueError("Diese Bestellung stammt nicht von Shopify.")

    data = graphql(account, FULFILLMENT_ORDERS_QUERY, {"id": f"gid://shopify/Order/{ref[len(REF_PREFIX):]}"})
    open_orders = [
        {"fulfillmentOrderId": n["id"]}
        for n in ((data.get("order") or {}).get("fulfillmentOrders") or {}).get("nodes") or []
        if n.get("status") in ("OPEN", "IN_PROGRESS")
    ]
    if not open_orders:
        raise ValueError("Shopify hat keine offene Lieferung für diese Bestellung (schon versendet?).")

    result = graphql(account, FULFILLMENT_CREATE, {"f": {
        "lineItemsByFulfillmentOrder": open_orders,
        "trackingInfo": {"number": tracking_number, "company": CARRIER_NAMES.get(carrier, carrier)},
        "notifyCustomer": True,
    }})
    errors = (result.get("fulfillmentCreate") or {}).get("userErrors") or []
    if errors:
        raise ValueError("; ".join(e.get("message", "?") for e in errors))

    order.tracking_number = tracking_number[:50]
    order.status = "shipped"
    db.commit()


# ---------------------------------------------------------------- push price / stock

PUSH_VARIANTS_QUERY = """
query($cursor: String) {
  productVariants(first: 100, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { id sku price inventoryQuantity product { id } inventoryItem { id } }
  }
}
"""
LOCATIONS_QUERY = "query { locations(first: 5) { nodes { id } } }"
PRICE_MUTATION = """
mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) { userErrors { field message } }
}
"""
STOCK_MUTATION = """
mutation($input: InventorySetQuantitiesInput!, $key: String!) {
  inventorySetQuantities(input: $input) @idempotent(key: $key) { userErrors { field message } }
}
"""


def _user_errors(result: dict, name: str) -> str | None:
    errors = (result.get(name) or {}).get("userErrors") or []
    return "; ".join(e.get("message", "?") for e in errors) or None


def plan_push(account: ShopifyAccount, products: dict, include_price: bool, include_quantity: bool,
              max_pages: int = 20) -> list[dict]:
    """What differs between NettoDesk (products by SKU) and the store. Changes nothing."""
    changes, cursor = [], None
    for _ in range(max_pages):
        page = graphql(account, PUSH_VARIANTS_QUERY, {"cursor": cursor}).get("productVariants") or {}
        for variant in page.get("nodes") or []:
            sku = variant.get("sku") or gid_number(variant.get("id"))
            product = products.get(sku)
            if not product:
                continue
            old_price = _money({"amount": variant.get("price")})
            new_price = None
            if include_price and product.selling_price is not None:
                candidate = Decimal(product.selling_price).quantize(Decimal("0.01"))
                if old_price != candidate:
                    new_price = candidate
            old_quantity = variant.get("inventoryQuantity")
            new_quantity = None
            if include_quantity and isinstance(old_quantity, int) and old_quantity != product.stock_quantity:
                new_quantity = max(0, int(product.stock_quantity))
            if new_price is None and new_quantity is None:
                continue
            changes.append({
                "sku": sku, "title": product.title,
                "variant_id": variant["id"], "product_id": (variant.get("product") or {}).get("id"),
                "inventory_item_id": (variant.get("inventoryItem") or {}).get("id"),
                "shop_price": None if old_price is None else str(old_price),
                "new_price": None if new_price is None else str(new_price),
                "shop_quantity": old_quantity, "new_quantity": new_quantity, "errors": [],
            })
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        cursor = info.get("endCursor")
    return changes


def apply_push(account: ShopifyAccount, changes: list[dict]) -> None:
    """Writes the planned changes to the store; failures are noted on each change."""
    by_product: dict = {}
    for change in changes:
        if change["new_price"] is not None:
            by_product.setdefault(change["product_id"], []).append(change)
    for product_id, group in by_product.items():
        try:
            error = _user_errors(graphql(account, PRICE_MUTATION, {
                "productId": product_id,
                "variants": [{"id": c["variant_id"], "price": c["new_price"]} for c in group]}),
                "productVariantsBulkUpdate")
        except (ValueError, httpx.HTTPError) as exc:
            error = str(exc)
        if error:
            for change in group:
                change["errors"].append(f"Preis: {error}")

    stock = [c for c in changes if c["new_quantity"] is not None]
    if not stock:
        return
    try:
        locations = (graphql(account, LOCATIONS_QUERY).get("locations") or {}).get("nodes") or []
    except (ValueError, httpx.HTTPError) as exc:
        locations, reason = [], str(exc)
    else:
        reason = "Der Shop hat mehrere Lagerorte; Mengen werden dann nicht gesendet."
    if len(locations) != 1:
        for change in stock:
            change["errors"].append(f"Menge: {reason}")
        return
    for change in stock:
        try:
            # changeFromQuantity makes Shopify refuse the write if the stock changed meanwhile.
            error = _user_errors(graphql(account, STOCK_MUTATION, {"key": str(uuid.uuid4()), "input": {
                "name": "available", "reason": "correction", "quantities": [{
                    "inventoryItemId": change["inventory_item_id"], "locationId": locations[0]["id"],
                    "quantity": change["new_quantity"], "changeFromQuantity": change["shop_quantity"]}]}}),
                "inventorySetQuantities")
        except (ValueError, httpx.HTTPError) as exc:
            error = str(exc)
        if error:
            change["errors"].append(f"Menge: {error}")


# ---------------------------------------------------------------- create a draft

CREATE_DRAFT_MUTATION = """
mutation($product: ProductCreateInput!, $media: [CreateMediaInput!]) {
  productCreate(product: $product, media: $media) {
    product { id variants(first: 1) { nodes { id } } }
    userErrors { field message }
  }
}
"""

DRAFT_VARIANT_MUTATION = """
mutation($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    productVariants { id }
    userErrors { field message }
  }
}
"""


def _draft_errors(payload) -> list[str]:
    return [str(e.get("message")) for e in (payload or {}).get("userErrors") or []]


def create_draft(account: ShopifyAccount, product: Product) -> dict:
    """Creates the product at Shopify as a DRAFT (invisible to customers).
    Title, picture, price and SKU are sent; stock is not."""
    existing, _ = fetch_variants(account)
    if any(v["sku"] == product.sku for v in existing):
        raise ValueError(f"Ein Shopify-Produkt mit der SKU '{product.sku}' gibt es schon.")
    image = product.image_url or ""
    media = [{"originalSource": image, "mediaContentType": "IMAGE"}] if image.startswith("https://") else []
    payload = graphql(account, CREATE_DRAFT_MUTATION,
                      {"product": {"title": product.title[:255], "status": "DRAFT"}, "media": media}).get("productCreate")
    errors = _draft_errors(payload)
    created = (payload or {}).get("product")
    if errors or not created:
        raise ValueError("; ".join(errors) or "Shopify hat kein Produkt angelegt.")
    variant = {"id": created["variants"]["nodes"][0]["id"], "inventoryItem": {"sku": product.sku}}
    if product.selling_price is not None:
        variant["price"] = str(product.selling_price)
    # The draft exists from here on, so a failure is a warning, not "nothing happened".
    try:
        result = graphql(account, DRAFT_VARIANT_MUTATION, {"productId": created["id"], "variants": [variant]})
        warnings = _draft_errors(result.get("productVariantsBulkUpdate"))
    except (ValueError, httpx.HTTPError) as exc:
        warnings = [str(exc)]
    return {
        "admin_url": f"https://{account.shop_domain}/admin/products/{gid_number(created['id'])}",
        "picture_sent": bool(media),
        "warnings": warnings,
    }
