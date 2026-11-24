"""
Pricing calculator: from unit cost + eBay fees + VAT mode + a profit target to a selling price.

Conventions (same as the accounting overview, see app/routers/accounting.py):
- The selling price, the purchase price, supplier shipping, packaging and the shipping
  label are GROSS amounts (they include VAT where VAT exists).
- vat_mode "kleinunternehmer": no VAT at all, every amount counts as it is.
- vat_mode "regular" (19%): the price contains VAT, so net revenue = price / 1.19.
  Input VAT on the goods and the label is deductible, so those costs count net (cost / 1.19).
  eBay fees are charged on the GROSS price and are NOT treated as deductible (this matches
  the accounting overview, which is the conservative choice).

  profit(P) = P/f - (goods + label)/f - (P * fee_pct + fee_fixed)      with f = 1 (no VAT) or 1.19
"""

from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")
HUNDRED = Decimal("100")

DEFAULT_VAT_RATE = Decimal("19")
DEFAULT_FEE_PERCENT = Decimal("11")
DEFAULT_FEE_FIXED = Decimal("0.35")


class PricingError(ValueError):
    """The target cannot be reached (fees + margin eat the whole price)."""


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _factor(vat_mode: str, vat_rate: Decimal) -> Decimal:
    return ONE + vat_rate / HUNDRED if vat_mode == "regular" else ONE


def breakdown(
    price: Decimal,
    *,
    goods_cost: Decimal,
    label_cost: Decimal,
    fee_percent: Decimal,
    fee_fixed: Decimal,
    vat_mode: str,
    vat_rate: Decimal,
) -> dict:
    """Everything that happens to one sale at a given gross price."""
    f = _factor(vat_mode, vat_rate)
    net_revenue = price / f
    sales_vat = price - net_revenue
    fees = price * fee_percent / HUNDRED + fee_fixed
    net_costs = (goods_cost + label_cost) / f
    input_vat = (goods_cost + label_cost) - net_costs
    profit = net_revenue - net_costs - fees
    margin = (profit / net_revenue * HUNDRED) if net_revenue > 0 else ZERO
    return {
        "price": str(money(price)),
        "net_revenue": str(money(net_revenue)),
        "sales_vat": str(money(sales_vat)),
        "ebay_fees": str(money(fees)),
        "goods_and_label_net": str(money(net_costs)),
        "input_vat": str(money(input_vat)),
        "vat_payable": str(money(sales_vat - input_vat)),
        "profit": str(money(profit)),
        "margin_percent": str(margin.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)),
    }


def solve_price(
    *,
    goods_cost: Decimal,
    label_cost: Decimal,
    fee_percent: Decimal,
    fee_fixed: Decimal,
    vat_mode: str,
    vat_rate: Decimal,
    target_profit: Decimal | None = None,
    target_margin_percent: Decimal | None = None,
) -> Decimal:
    """Smallest gross price (rounded UP to the cent) that reaches the target.

    With neither target given this is the break-even price (profit 0).
    A margin target is a percentage of NET revenue.
    """
    f = _factor(vat_mode, vat_rate)
    fee_pct = fee_percent / HUNDRED
    net_costs = (goods_cost + label_cost) / f
    margin = (target_margin_percent or ZERO) / HUNDRED
    profit = target_profit or ZERO

    # profit = P/f - net_costs - P*fee_pct - fee_fixed, and profit = profit_target + margin * P/f
    denominator = (ONE - margin) / f - fee_pct
    if denominator <= 0:
        raise PricingError("Mit diesen Gebühren und diesem Ziel ist kein Preis möglich (Marge zu hoch).")
    price = (profit + net_costs + fee_fixed) / denominator
    return price.quantize(CENT, rounding=ROUND_CEILING)


def round_up_to_ending(price: Decimal, ending: str) -> Decimal:
    """Round UP to the next price ending: 'x.99', 'x.49' (every half euro .49/.99) or 'x.90'."""
    if ending == "x.99":
        steps = [Decimal("0.99")]
    elif ending == "x.49":
        steps = [Decimal("0.49"), Decimal("0.99")]
    elif ending == "x.90":
        steps = [Decimal("0.90")]
    else:
        return price
    whole = price.to_integral_value(rounding="ROUND_FLOOR")
    for base in (whole - 1, whole, whole + 1):
        for step in steps:
            candidate = base + step
            if candidate >= price and candidate >= 0:
                return candidate
    return price
