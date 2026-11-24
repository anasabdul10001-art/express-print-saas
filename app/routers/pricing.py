"""Pricing calculator endpoints (see app/services/pricing.py for the math and conventions)."""

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload

from app.core.deps import get_current_user, get_db
from app.models.order import Order
from app.models.product import Product
from app.models.tenant import Tenant
from app.models.user import User
from app.services import pricing

router = APIRouter(prefix="/pricing", tags=["pricing"])



class PricingParams(BaseModel):
    # None = use the tenant's saved VAT setting.
    vat_mode: Literal["kleinunternehmer", "regular"] | None = None
    label_cost: Decimal = Field(default=Decimal("1.10"), ge=0, le=1000)
    fee_percent: Decimal = Field(default=pricing.DEFAULT_FEE_PERCENT, ge=0, lt=100)
    fee_fixed: Decimal = Field(default=pricing.DEFAULT_FEE_FIXED, ge=0, le=1000)
    # Exactly one target; with neither, the result is the break-even price.
    target_profit: Decimal | None = Field(default=None, ge=0, le=100000)
    target_margin_percent: Decimal | None = Field(default=None, ge=0, lt=100)
    round_to: Literal["none", "x.99", "x.49", "x.90"] = "none"


class CalculateIn(PricingParams):
    purchase_price: Decimal = Field(default=Decimal("0"), ge=0, le=100000)
    supplier_shipping_cost: Decimal = Field(default=Decimal("0"), ge=0, le=100000)
    packaging_cost: Decimal = Field(default=Decimal("0"), ge=0, le=100000)
    # Optional: evaluate an existing price instead of / in addition to solving for one.
    check_price: Decimal | None = Field(default=None, ge=0, le=100000)


def _vat_mode(db: Session, user: User, requested: str | None) -> str:
    if requested:
        return requested
    return db.query(Tenant).filter(Tenant.id == user.tenant_id).one().vat_mode


def _common(params: PricingParams, vat_mode: str, fee_percent: Decimal | None = None) -> dict:
    return dict(
        label_cost=params.label_cost,
        fee_percent=params.fee_percent if fee_percent is None else fee_percent,
        fee_fixed=params.fee_fixed,
        vat_mode=vat_mode,
        vat_rate=pricing.DEFAULT_VAT_RATE,
    )


def _solve(goods: Decimal, params: PricingParams, vat_mode: str, fee_percent: Decimal | None = None) -> dict:
    if params.target_profit is not None and params.target_margin_percent is not None:
        raise HTTPException(status_code=400, detail="Bitte nur ein Ziel angeben: Gewinn in Euro ODER Marge in Prozent.")
    common = _common(params, vat_mode, fee_percent)
    try:
        break_even = pricing.solve_price(goods_cost=goods, **common)
        suggested = pricing.solve_price(
            goods_cost=goods,
            target_profit=params.target_profit,
            target_margin_percent=params.target_margin_percent,
            **common,
        )
    except pricing.PricingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    suggested = pricing.round_up_to_ending(suggested, params.round_to)
    return {
        "break_even_price": str(break_even),
        "suggested_price": str(suggested),
        "at_suggested": pricing.breakdown(suggested, goods_cost=goods, **common),
    }


@router.post("/calculate")
def calculate(payload: CalculateIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vat_mode = _vat_mode(db, current_user, payload.vat_mode)
    goods = payload.purchase_price + payload.supplier_shipping_cost + payload.packaging_cost
    result = {"vat_mode": vat_mode, "vat_rate": str(pricing.DEFAULT_VAT_RATE), "goods_cost": str(pricing.money(goods))}
    result.update(_solve(goods, payload, vat_mode))
    if payload.check_price is not None:
        result["at_check_price"] = pricing.breakdown(payload.check_price, goods_cost=goods, **_common(payload, vat_mode))
    return result


def _product_fee(product: Product, orders: list, params: PricingParams) -> tuple[Decimal, str, Decimal | None, int]:
    """(fee percent to use, source, observed percent, observed order count). Manual > observed > default."""
    observed, count = pricing.observed_fee_percent(orders, product.id, params.fee_fixed)
    if product.ebay_fee_percent is not None:
        return product.ebay_fee_percent, "product", observed, count
    if observed is not None:
        return observed, "observed", observed, count
    return params.fee_percent, "default", None, 0


def _orders_by_product(db: Session, user: User) -> dict:
    orders = (
        db.query(Order)
        .options(joinedload(Order.items))
        .filter(Order.tenant_id == user.tenant_id)
        .all()
    )
    grouped: dict = {}
    for order in orders:
        if len(order.items) == 1 and order.items[0].product_id is not None:
            grouped.setdefault(order.items[0].product_id, []).append(order)
    return grouped


@router.get("/fee-info/{product_id}")
def fee_info(product_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Which eBay fee percent applies to one product, and where it comes from."""
    product = db.query(Product).filter(Product.id == product_id, Product.tenant_id == current_user.tenant_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Produkt nicht gefunden")
    params = PricingParams()
    fee, source, observed, count = _product_fee(product, _orders_by_product(db, current_user).get(product.id, []), params)
    return {"fee_percent": str(fee), "source": source, "observed_percent": str(observed) if observed is not None else None, "observed_orders": count}


@router.post("/products")
def price_all_products(params: PricingParams, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Suggested price for every active product that has a known unit cost, next to its current price.

    The eBay fee percent is per product: the value stored on the product, else the rate observed on its
    past orders, else params.fee_percent (the default).
    """
    vat_mode = _vat_mode(db, current_user, params.vat_mode)
    products = (
        db.query(Product)
        .filter(Product.tenant_id == current_user.tenant_id, Product.active.is_(True))
        .order_by(Product.title)
        .all()
    )
    orders = _orders_by_product(db, current_user)
    rows = []
    for product in products:
        goods = product.unit_cost
        if goods is None:
            continue
        fee, source, observed, count = _product_fee(product, orders.get(product.id, []), params)
        solved = _solve(goods, params, vat_mode, fee)
        row = {
            "id": str(product.id),
            "sku": product.sku,
            "title": product.title,
            "goods_cost": str(pricing.money(goods)),
            "fee_percent": str(fee),
            "fee_source": source,
            "observed_fee_percent": str(observed) if observed is not None else None,
            "observed_orders": count,
            "current_price": str(product.selling_price) if product.selling_price is not None else None,
            "break_even_price": solved["break_even_price"],
            "suggested_price": solved["suggested_price"],
            "at_suggested": solved["at_suggested"],
            "at_current": None,
        }
        if product.selling_price is not None:
            row["at_current"] = pricing.breakdown(product.selling_price, goods_cost=goods, **_common(params, vat_mode, fee))
        rows.append(row)
    return {"vat_mode": vat_mode, "vat_rate": str(pricing.DEFAULT_VAT_RATE), "products": rows}
