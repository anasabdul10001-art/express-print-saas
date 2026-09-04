from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.product import Product
from app.models.user import User
from app.schemas.product import ProductCreate, ProductOut, ProductUpdate

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
