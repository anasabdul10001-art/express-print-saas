import math
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.purchase import PurchaseLine
from app.models.stock_movement import StockMovement
from app.models.supplier_item import SupplierItem
from app.models.user import User
from app.services.ebay_csv_import import CsvImportError, parse_active_listings_csv
from app.schemas.product import ProductCreate, ProductOut, ProductUpdate, RestockCreate, StockMovementOut

router = APIRouter(prefix="/products", tags=["products"])


def _with_profit(product: Product) -> ProductOut:
    """
    Computes profit_per_unit and margin_percent server-side so the frontend
    never has to (and can never show a number that disagrees with the
    backend). Margin = profit / selling_price - NOT profit / cost
    (that's markup, a different number - see project notes).
    """
    out = ProductOut.model_validate(product)
    if product.selling_price is not None and product.unit_cost is not None:
        profit = product.selling_price - product.unit_cost
        out.profit_per_unit = profit
        if product.selling_price != 0:
            out.margin_percent = (profit / product.selling_price * 100).quantize(Decimal("0.01"))
    return out


MAX_IMPORT_BYTES = 2 * 1024 * 1024


class BulkPriceIn(BaseModel):
    product_ids: list[str] | None = None  # None = all active products
    mode: Literal["percent", "amount"]
    value: Decimal = Field(..., description="Negative lowers the price, positive raises it")
    round_to_99: bool = False
    dry_run: bool = True


def _new_price(old: Decimal, mode: str, value: Decimal, round_to_99: bool) -> Decimal:
    new = old * (1 + value / 100) if mode == "percent" else old + value
    if round_to_99:
        new = Decimal(math.floor(new + Decimal("0.5"))) - Decimal("0.01")
    new = max(new, Decimal("0.01"))
    return new.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@router.post("/bulk-price")
def bulk_price(
    payload: BulkPriceIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Raises/lowers the selling price of many products at once. Only changes
    the price in ShipSync - nothing is sent to eBay. dry_run=True only previews."""
    if payload.value == 0:
        raise HTTPException(status_code=400, detail="Der Wert darf nicht 0 sein.")
    query = db.query(Product).filter(Product.tenant_id == current_user.tenant_id, Product.active.is_(True))
    if payload.product_ids is not None:
        if not payload.product_ids:
            raise HTTPException(status_code=400, detail="Keine Produkte ausgewählt.")
        query = query.filter(Product.id.in_(payload.product_ids))
    changes, skipped = [], 0
    for product in query.all():
        if product.selling_price is None:
            skipped += 1
            continue
        new = _new_price(Decimal(product.selling_price), payload.mode, payload.value, payload.round_to_99)
        changes.append({"id": str(product.id), "sku": product.sku, "title": product.title,
                        "old_price": str(product.selling_price), "new_price": str(new)})
        if not payload.dry_run:
            product.selling_price = new
    if not payload.dry_run:
        db.commit()
    return {"applied": not payload.dry_run, "changed": len(changes), "skipped_no_price": skipped, "items": changes}


@router.post("/import-ebay-csv")
async def import_ebay_csv(
    file: UploadFile,
    dry_run: bool = Query(True, description="True = only report what would change, write nothing"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Imports the 'Active listings' CSV downloaded from eBay Seller Hub.
    Read-only towards eBay (the seller downloads the file himself). Matches
    existing products by SKU: updates title, selling price, stock and image,
    NEVER touches purchase_price/shelf/package data; creates the rest.
    Default is a dry run so the seller sees the result before anything is written.
    """
    content = await file.read()
    if len(content) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="Datei zu groß (max. 2 MB).")
    try:
        parsed = parse_active_listings_csv(content)
    except CsvImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    existing = {
        p.sku: p
        for p in db.query(Product).filter(
            Product.tenant_id == current_user.tenant_id,
            Product.sku.in_([item["sku"] for item in parsed["products"]]),
        )
    }
    created = updated = unchanged = 0
    for item in parsed["products"]:
        product = existing.get(item["sku"])
        if product is None:
            created += 1
            if not dry_run:
                stock = item["stock_quantity"] or 0
                product = Product(
                    tenant_id=current_user.tenant_id, sku=item["sku"], title=item["title"],
                    selling_price=item["selling_price"], stock_quantity=stock, image_url=item["image_url"],
                )
                db.add(product)
                db.flush()
                if stock:
                    db.add(StockMovement(
                        tenant_id=current_user.tenant_id, product_id=product.id, quantity_change=stock,
                        reason="manual_correction", reference="ebay-csv-import",
                    ))
            continue

        changes = {}
        if product.title != item["title"]:
            changes["title"] = item["title"]
        if item["selling_price"] is not None and product.selling_price != item["selling_price"]:
            changes["selling_price"] = item["selling_price"]
        if item["image_url"] and product.image_url != item["image_url"]:
            changes["image_url"] = item["image_url"]
        if not product.active:
            changes["active"] = True
        new_stock = item["stock_quantity"]
        if product.unlisted:
            changes["unlisted"] = False
            new_stock = None
        if new_stock is not None and product.stock_quantity != new_stock:
            changes["stock_quantity"] = new_stock
        if not changes:
            unchanged += 1
            continue
        updated += 1
        if not dry_run:
            delta = changes.get("stock_quantity", product.stock_quantity) - product.stock_quantity
            for field, value in changes.items():
                setattr(product, field, value)
            if delta:
                db.add(StockMovement(
                    tenant_id=current_user.tenant_id, product_id=product.id, quantity_change=delta,
                    reason="manual_correction", reference="ebay-csv-import",
                ))
    if not dry_run:
        db.commit()

    return {
        "dry_run": dry_run,
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "skipped": parsed["skipped"],
        "warnings": parsed["warnings"][:50],
    }


@router.get("", response_model=list[ProductOut])
def list_products(
    search: str | None = Query(None, description="Matches SKU or title"),
    active: bool | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(Product).filter(Product.tenant_id == current_user.tenant_id, Product.merged_into.is_(None))
    if active is not None:
        query = query.filter(Product.active == active)
    if search:
        like = f"%{search}%"
        query = query.filter((Product.title.ilike(like)) | (Product.sku.ilike(like)))
    products = query.order_by(Product.created_at.desc()).all()
    sold = dict(
        db.query(OrderItem.product_id, func.sum(OrderItem.quantity))
        .join(Order, Order.id == OrderItem.order_id)
        .filter(Order.tenant_id == current_user.tenant_id, Order.status != "canceled", OrderItem.product_id.isnot(None))
        .group_by(OrderItem.product_id)
        .all()
    )
    out = []
    for p in products:
        item = _with_profit(p)
        item.sold_quantity = int(sold.get(p.id) or 0)
        out.append(item)
    return out


@router.post("", response_model=ProductOut, status_code=201)
def create_product(
    payload: ProductCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    duplicate = (
        db.query(Product)
        .filter(Product.tenant_id == current_user.tenant_id, Product.sku == payload.sku)
        .first()
    )
    if duplicate:
        raise HTTPException(status_code=409, detail=f"SKU '{payload.sku}' existiert bereits.")

    product = Product(tenant_id=current_user.tenant_id, **payload.model_dump())
    db.add(product)
    db.commit()
    db.refresh(product)
    return _with_profit(product)


COST_FIELDS = ("purchase_price", "supplier_shipping_cost", "packaging_cost")


class LinkIn(BaseModel):
    """Joins stock goods with the product that came from the listing."""
    listing_product_id: uuid.UUID
    # How many pieces there really are. Asked, never added up: the listing may already count them.
    stock_quantity: int = Field(..., ge=0, le=1000000)
    cost_from: Literal["stock", "listing"] | None = None


@router.post("/{product_id}/link", response_model=ProductOut)
def link_stock_to_listing(
    product_id: uuid.UUID,
    payload: LinkIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """One product remains: the listing's (SKU, title, picture, orders). It takes over the
    cost, the purchases, the supplier links and the history of the stock goods."""
    def own(pid):
        return (db.query(Product).filter(Product.id == pid, Product.tenant_id == current_user.tenant_id,
                                         Product.merged_into.is_(None)).with_for_update().first())

    stock, listing = own(product_id), own(payload.listing_product_id)
    if not stock or not listing:
        raise HTTPException(status_code=404, detail="Produkt nicht gefunden")
    if stock.id == listing.id or not stock.unlisted or not stock.active:
        raise HTTPException(status_code=400, detail="Verknüpfen geht nur von Lagerware, die noch nicht angeboten wird.")
    if listing.unlisted or not listing.active:
        raise HTTPException(status_code=400, detail="Bitte ein aktives, angebotenes Produkt wählen.")

    both_known = stock.unit_cost is not None and listing.unit_cost is not None
    differ = both_known and any((getattr(stock, f) or 0) != (getattr(listing, f) or 0) for f in COST_FIELDS)
    if differ and payload.cost_from is None:
        raise HTTPException(status_code=422, detail="Beide Produkte haben unterschiedliche Kosten. "
                            "Bitte wählen, welche gelten.")
    if stock.unit_cost is not None and (listing.unit_cost is None or payload.cost_from == "stock"):
        for field in COST_FIELDS:
            setattr(listing, field, getattr(stock, field))

    for model in (PurchaseLine, SupplierItem, StockMovement, OrderItem):
        db.query(model).filter(model.product_id == stock.id).update(
            {model.product_id: listing.id}, synchronize_session=False)
    change = payload.stock_quantity - listing.stock_quantity
    if change:
        db.add(StockMovement(tenant_id=current_user.tenant_id, product_id=listing.id, quantity_change=change,
                             reason="manual_correction", reference=f"Verknüpft mit Lagerware {stock.sku}"[:255]))
    listing.stock_quantity = payload.stock_quantity
    for field in ("shelf_location", "image_url", "acquired_on"):
        if not getattr(listing, field):
            setattr(listing, field, getattr(stock, field))
    # as when a cost is typed in by hand: fill only sales that have no cost yet
    if listing.unit_cost is not None:
        db.query(OrderItem).filter(OrderItem.product_id == listing.id, OrderItem.purchase_price.is_(None)
                                   ).update({OrderItem.purchase_price: listing.unit_cost}, synchronize_session=False)
    stock.merged_into, stock.active, stock.unlisted, stock.stock_quantity = listing.id, False, False, 0
    db.commit()
    db.refresh(listing)
    return _with_profit(listing)


@router.get("/{product_id}", response_model=ProductOut)
def get_product(
    product_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    product = (
        db.query(Product)
        .filter(Product.id == product_id, Product.tenant_id == current_user.tenant_id)
        .first()
    )
    if not product:
        raise HTTPException(status_code=404, detail="Produkt nicht gefunden")
    return _with_profit(product)


@router.patch("/{product_id}", response_model=ProductOut)
def update_product(
    product_id: str,
    payload: ProductUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    product = (
        db.query(Product)
        .filter(Product.id == product_id, Product.tenant_id == current_user.tenant_id)
        .first()
    )
    if not product:
        raise HTTPException(status_code=404, detail="Produkt nicht gefunden")

    update_data = payload.model_dump(exclude_unset=True)
    if update_data.get("unlisted") is None:
        update_data.pop("unlisted", None)

    new_sku = update_data.get("sku")
    if new_sku and new_sku != product.sku:
        duplicate = (
            db.query(Product)
            .filter(
                Product.tenant_id == current_user.tenant_id,
                Product.sku == new_sku,
                Product.id != product.id,
            )
            .first()
        )
        if duplicate:
            raise HTTPException(status_code=409, detail=f"SKU '{new_sku}' existiert bereits.")

    for field, value in update_data.items():
        setattr(product, field, value)

    # Orders imported before the cost was known have no cost snapshot, so
    # their profit was blank. Fill those (only those) now; costs already
    # snapshotted on past orders are never rewritten.
    if product.unit_cost is not None:
        db.query(OrderItem).filter(
            OrderItem.product_id == product.id, OrderItem.purchase_price.is_(None)
        ).update({OrderItem.purchase_price: product.unit_cost}, synchronize_session=False)

    db.commit()
    db.refresh(product)
    return _with_profit(product)


@router.delete("/{product_id}", status_code=204)
def deactivate_product(
    product_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Soft delete: sets active=False rather than removing the row. Order
    items can reference a product's id (see models/order.py), so a hard
    delete would either fail on the foreign key or silently orphan order
    history - neither is acceptable. A deactivated product simply stops
    showing up by default and can't be picked for new orders.
    """
    product = (
        db.query(Product)
        .filter(Product.id == product_id, Product.tenant_id == current_user.tenant_id)
        .first()
    )
    if not product:
        raise HTTPException(status_code=404, detail="Produkt nicht gefunden")

    product.active = False
    db.commit()


@router.post("/{product_id}/restock", response_model=ProductOut)
def restock_product(
    product_id: str,
    payload: RestockCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Adds stock via a dedicated endpoint (not a plain PATCH to stock_quantity)
    so every increase is always logged as a StockMovement with reason
    "restock" - a PATCH would silently overwrite the number with no record
    of why it changed.
    """
    product = (
        db.query(Product)
        .filter(Product.id == product_id, Product.tenant_id == current_user.tenant_id)
        .first()
    )
    if not product:
        raise HTTPException(status_code=404, detail="Produkt nicht gefunden")

    product.stock_quantity += payload.quantity
    db.add(StockMovement(
        tenant_id=current_user.tenant_id,
        product_id=product.id,
        quantity_change=payload.quantity,
        reason="restock",
        reference=None,
    ))
    db.commit()
    db.refresh(product)
    return _with_profit(product)


@router.get("/{product_id}/movements", response_model=list[StockMovementOut])
def list_stock_movements(
    product_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Full movement history for one product, newest first."""
    product = (
        db.query(Product)
        .filter(Product.id == product_id, Product.tenant_id == current_user.tenant_id)
        .first()
    )
    if not product:
        raise HTTPException(status_code=404, detail="Produkt nicht gefunden")

    movements = (
        db.query(StockMovement)
        .filter(StockMovement.product_id == product_id, StockMovement.tenant_id == current_user.tenant_id)
        .order_by(StockMovement.created_at.desc())
        .all()
    )
    return movements
