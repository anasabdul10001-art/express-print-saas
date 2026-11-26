"""Stock goods stop being "not listed" once a listing or a sale with their SKU shows up.

Only an identical SKU (or item number) does this; anything else is linked by the
seller. The quantity is never changed here: the same goods must not be counted twice.
"""

from sqlalchemy.orm import Session

from app.models.product import Product


def now_listed(db: Session, product: Product, image_url: str | None = None, selling_price=None) -> bool:
    """True if this was a stock product that is now known to be offered."""
    if not product.unlisted:
        return False
    product.unlisted = False
    product.image_url = product.image_url or image_url
    if product.selling_price is None:
        product.selling_price = selling_price
    return True


def seen_in_shop(db: Session, tenant_id, offers: list[tuple]) -> int:
    """offers: (sku or item number, image_url, price) of listings an import found."""
    waiting = {p.sku: p for p in db.query(Product).filter(
        Product.tenant_id == tenant_id, Product.unlisted.is_(True), Product.active.is_(True))}
    found = 0
    for key, image_url, price in offers:
        if key in waiting and now_listed(db, waiting[key], image_url, price):
            found += 1
    return found
