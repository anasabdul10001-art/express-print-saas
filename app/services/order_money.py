"""One place for what an order is worth, so Orders and Accounting always agree.

- revenue : what the buyer paid according to eBay (items - discount + shipping); falls back to
            the item prices for manual orders / orders imported before this was stored.
- labels  : shipping label bought through eBay.
- fees    : eBay fees; a FULLY refunded order gets its fees back from eBay, so they count as 0.
- refund  : money paid back to the buyer.
- goods   : cost of the goods; not a loss when the item came back to stock after a refund.
"""
from decimal import Decimal

ZERO = Decimal("0")


def revenue(order) -> Decimal:
    if order.ebay_buyer_total is not None:
        return order.ebay_buyer_total
    return sum(((i.unit_price or ZERO) * i.quantity for i in order.items), ZERO)


def fees(order) -> Decimal:
    """eBay fees minus what eBay gave back on a refund (a fully refunded order ends at 0)."""
    if order.ebay_fee_credit is not None:
        return max((order.ebay_fees or ZERO) - order.ebay_fee_credit, ZERO)
    if order.ebay_payment_status == "FULLY_REFUNDED":
        return ZERO
    return order.ebay_fees or ZERO


def labels(order) -> Decimal:
    """What the seller paid for the shipping label (eBay Finances API); kept even if refunded."""
    return order.shipping_label_cost or ZERO


def refund(order) -> Decimal:
    return order.refund_amount or ZERO


def goods(order) -> Decimal:
    if order.item_returned_to_stock and refund(order) > 0:
        return ZERO
    return sum(((i.purchase_price or ZERO) * i.quantity for i in order.items), ZERO)


def profit(order) -> Decimal:
    """Net result of one order by the same rules the accounting page uses."""
    return (
        revenue(order) - refund(order) - fees(order) - goods(order) - labels(order)
        - (order.return_shipping_cost or ZERO)
    )
