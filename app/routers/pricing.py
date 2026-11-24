"""Pricing calculator endpoints (see app/services/pricing.py for the math and conventions)."""

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
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


def _common(params: PricingParams, vat_mode: str) -> dict:
    return dict(
        label_cost=params.label_cost,
        fee_percent=params.fee_percent,
        fee_fixed=params.fee_fixed,
        vat_mode=vat_mode,
        vat_rate=pricing.DEFAULT_VAT_RATE,
    )


def _solve(goods: Decimal, params: PricingParams, vat_mode: str) -> dict:
    if params.target_profit is not None and params.target_margin_percent is not None:
        raise HTTPException(status_code=400, detail="Bitte nur ein Ziel angeben: Gewinn in Euro ODER Marge in Prozent.")
    common = _common(params, vat_mode)
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


@router.post("/products")
def price_all_products(params: PricingParams, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Suggested price for every active product that has a known unit cost, next to its current price."""
    vat_mode = _vat_mode(db, current_user, params.vat_mode)
    common = _common(params, vat_mode)
    products = (
        db.query(Product)
        .filter(Product.tenant_id == current_user.tenant_id, Product.active.is_(True))
        .order_by(Product.title)
        .all()
    )
    rows = []
    for product in products:
        goods = product.unit_cost
        if goods is None:
            continue
        solved = _solve(goods, params, vat_mode)
        row = {
            "id": str(product.id),
            "sku": product.sku,
            "title": product.title,
            "goods_cost": str(pricing.money(goods)),
            "current_price": str(product.selling_price) if product.selling_price is not None else None,
            "break_even_price": solved["break_even_price"],
            "suggested_price": solved["suggested_price"],
            "at_suggested": solved["at_suggested"],
            "at_current": None,
        }
        if product.selling_price is not None:
            row["at_current"] = pricing.breakdown(product.selling_price, goods_cost=goods, **common)
        rows.append(row)
    return {"vat_mode": vat_mode, "vat_rate": str(pricing.DEFAULT_VAT_RATE), "products": rows}
