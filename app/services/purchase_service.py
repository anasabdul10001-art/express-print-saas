"""
Weighted average unit cost.

    new average = (pieces in stock * old cost + new pieces * new cost) / (all pieces)

The goods price goes into Product.purchase_price and the invoice's extra costs
(shipping, customs ...), split evenly over all pieces on the invoice, into
Product.supplier_shipping_cost. Packaging cost is not touched. Orders already
imported keep the cost they were booked with.
"""

from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal("0")
CENT = Decimal("0.01")


def average(old_quantity: int, old_cost: Decimal | None, new_quantity: int, new_cost: Decimal) -> Decimal:
    """Without a known old cost (or without old stock) the new cost is the average."""
    if old_cost is None or old_quantity <= 0:
        result = new_cost
    else:
        result = (old_quantity * old_cost + new_quantity * new_cost) / (old_quantity + new_quantity)
    return result.quantize(CENT, rounding=ROUND_HALF_UP)


def old_pieces(stock: int, new_quantity: int, add_to_stock: bool) -> int:
    """How many pieces carry the old cost. If the new goods are already counted in the
    stock (add_to_stock off), they have to be taken out first."""
    return max(stock if add_to_stock else stock - new_quantity, 0)
