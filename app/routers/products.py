from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.product import Product
from app.models.stock_movement import StockMovement
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
    if product.selling_price is not None and product.purchase_price is not None:
        profit = product.selling_price - product.purchase_price
        out.profit_per_unit = profit
        if product.selling_price != 0:
            out.margin_percent = (profit / product.selling_price * 100).quantize(Decimal("0.01"))
    return out


MAX_IMPORT_BYTES = 2 * 1024 * 1024


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
    query = db.query(Product).filter(Product.tenant_id == current_user.tenant_id)
    if active is not None:
        query = query.filter(Product.active == active)
    if search:
        like = f"%{search}%"
        query = query.filter((Product.title.ilike(like)) | (Product.sku.ilike(like)))
    products = query.order_by(Product.created_at.desc()).all()
    return [_with_profit(p) for p in products]


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
