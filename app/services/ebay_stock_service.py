"""Takes the available quantities of the active eBay listings over into ShipSync.

One direction only: eBay is read, ShipSync is written. The import sets a
product's stock once and never touches it again, so a restock done on eBay
would otherwise never arrive here.
"""

from sqlalchemy.orm import Session

from app.models.product import Product
from app.models.stock_movement import StockMovement

REASON = "ebay_stock"


def match(db: Session, tenant_id, listings: list[dict]) -> tuple[list, dict]:
    """Matches listings to products (listing SKU, else item number). Returns all
    offered products and {product id: (product, its listings)}."""
    products = db.query(Product).filter(
        Product.tenant_id == tenant_id, Product.active.is_(True),
        Product.unlisted.is_(False), Product.merged_into.is_(None)).all()
    by_sku = {p.sku: p for p in products}

    found: dict = {}
    for listing in listings:
        product = by_sku.get(listing["sku"] or "") or by_sku.get(listing["item_id"])
        if product:
            found.setdefault(product.id, (product, []))[1].append(listing)
    return products, found


def compare(db: Session, tenant_id, listings: list[dict]) -> dict:
    """The products whose quantity differs from their listing. A product matched by
    more than one listing is only reported: it is unknown whether those listings
    share the same goods."""
    products, found = match(db, tenant_id, listings)

    items, ambiguous, equal = [], [], 0
    for product, matches in found.values():
        row = {"product_id": str(product.id), "sku": product.sku, "title": product.title}
        if len(matches) > 1:
            ambiguous.append({**row, "item_ids": [m["item_id"] for m in matches]})
        elif matches[0]["available"] == product.stock_quantity:
            equal += 1
        else:
            items.append({**row, "item_id": matches[0]["item_id"],
                          "shipsync_quantity": product.stock_quantity,
                          "ebay_quantity": matches[0]["available"]})
    items.sort(key=lambda r: r["title"].lower())
    return {"items": items, "ambiguous": ambiguous, "equal": equal,
            "without_listing": len(products) - len(found)}


def apply(db: Session, tenant_id, items: list[dict], confirmed: dict[str, int]) -> tuple[int, list[str]]:
    """confirmed: {product_id: eBay quantity the seller saw}. A product is only
    changed while eBay still reports exactly that number. Returns (updated, ids
    left unchanged because the numbers moved in the meantime)."""
    current = {row["product_id"]: row for row in items}
    updated, outdated = 0, []
    for product_id, quantity in confirmed.items():
        row = current.get(product_id)
        if row is None or row["ebay_quantity"] != quantity:
            outdated.append(product_id)
            continue
        product = db.query(Product).filter(Product.tenant_id == tenant_id, Product.id == product_id).one()
        db.add(StockMovement(tenant_id=tenant_id, product_id=product.id,
                             quantity_change=quantity - product.stock_quantity,
                             reason=REASON, reference=row["item_id"]))
        product.stock_quantity = quantity
        updated += 1
    db.commit()
    return updated, outdated
