"""eBay's own money figures drive revenue, refunds and fees (reconciled against Seller Hub)."""
from decimal import Decimal

from app.models.order import Order, OrderItem
from app.services import order_money
from app.services.ebay_order_service import _apply_amounts, _unit_price


def _fully_refunded_with_discount():
    # real order 21-15411-18885: 4 x 2.49, multi-buy discount -2.52, fully refunded
    return {
        "orderPaymentStatus": "FULLY_REFUNDED",
        "pricingSummary": {"priceSubtotal": {"value": "9.96"}, "priceDiscount": {"value": "-2.52"}, "total": {"value": "7.44"}},
        "paymentSummary": {"refunds": [{"amount": {"value": "5.96"}}], "totalDueSeller": {"value": "0.0"}},
    }


def test_fully_refunded_order_nets_to_zero():
    order = Order(ebay_fees=Decimal("1.48"))
    order.items = [OrderItem(quantity=4, unit_price=Decimal("2.49"), purchase_price=Decimal("0"))]
    _apply_amounts(order, _fully_refunded_with_discount())
    assert order.ebay_buyer_total == Decimal("7.44") and order.ebay_payout == Decimal("0.00")
    assert order.refund_amount == Decimal("7.44")  # buyer got everything back, not eBay's fee-net 5.96
    assert order_money.fees(order) == 0  # eBay returned the fee
    assert order_money.revenue(order) - order_money.refund(order) - order_money.fees(order) == 0


def test_partial_refund_keeps_fee_and_shipping_counts_as_revenue():
    # real order 20-15414-71177: 2.99 + 1.10 shipping, refund 0.94, fee 1.00, payout 2.15
    order = Order(ebay_fees=Decimal("1.00"))
    order.items = [OrderItem(quantity=1, unit_price=Decimal("2.99"), purchase_price=Decimal("0"))]
    _apply_amounts(order, {
        "orderPaymentStatus": "PARTIALLY_REFUNDED",
        "pricingSummary": {"total": {"value": "4.09"}},
        "paymentSummary": {"refunds": [{"amount": {"value": "0.94"}}], "totalDueSeller": {"value": "2.15"}},
    })
    assert order_money.revenue(order) == Decimal("4.09")
    assert order.refund_amount == Decimal("0.94")
    assert order_money.revenue(order) - order_money.refund(order) - order_money.fees(order) == Decimal("2.15")


def test_unit_price_is_actual_line_price():
    assert _unit_price({"lineItemCost": {"value": "5.78"}}, 2) == Decimal("2.89")


def test_manual_order_falls_back_to_item_prices():
    order = Order()
    order.items = [OrderItem(quantity=2, unit_price=Decimal("3.00"))]
    assert order_money.revenue(order) == 6
