"""Finances sync: refunds with fee credit, shipping labels, non-order fees as expenses (real eBay shapes)."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from app.database import SessionLocal
from app.models.expense import Expense
from app.models.order import Order, OrderItem
from app.services import ebay_order_service, order_money


def _tx(kind):
    return {
        "SALE": [
            {"orderId": "21-1", "totalFeeAmount": {"value": "1.48"}},
            {"orderId": "20-2", "totalFeeAmount": {"value": "1.00"}},
        ],
        "REFUND": [  # real shapes: amount is eBay's net refund, totalFeeAmount the fee given back
            {"orderId": "21-1", "bookingEntry": "DEBIT", "amount": {"value": "5.96"}, "totalFeeAmount": {"value": "1.48"}},
            {"orderId": "20-2", "bookingEntry": "DEBIT", "amount": {"value": "0.94"}, "totalFeeAmount": {"value": "0.16"}},
        ],
        "SHIPPING_LABEL": [
            {"orderId": "21-1", "bookingEntry": "DEBIT", "amount": {"value": "1.1"}},
            {"orderId": "20-2", "bookingEntry": "DEBIT", "amount": {"value": "1.8"}},
        ],
        "NON_SALE_CHARGE": [
            {"transactionId": "FEE-1", "bookingEntry": "DEBIT", "amount": {"value": "0.42"},
             "transactionMemo": "Insertion Fee", "transactionDate": "2026-11-23T16:19:50.039Z"},
        ],
    }[kind]


def _run(tenant_id):
    with SessionLocal() as db, patch.object(
        ebay_order_service, "_fetch_transactions", side_effect=lambda a, d, t, kind, n: _tx(kind)
    ):
        ebay_order_service.sync_fees(object(), tenant_id, db, "tok", [])


def test_sync_stores_refund_credit_labels_and_imports_fees_once(client, register):
    headers, _ = register()
    tenant_id = uuid.UUID(client.get("/auth/me", headers=headers).json()["tenant_id"])
    with SessionLocal() as db:
        for ref, price in (("21-1", "7.44"), ("20-2", "4.09")):
            db.add(Order(tenant_id=tenant_id, external_order_ref=ref, status="new", ebay_buyer_total=Decimal(price),
                         ordered_at=datetime(2026, 11, 20, tzinfo=timezone.utc)))
        db.commit()
    _run(tenant_id)
    _run(tenant_id)  # second run must not duplicate the imported fee
    with SessionLocal() as db:
        full = db.query(Order).filter(Order.external_order_ref == "21-1").one()
        partial = db.query(Order).filter(Order.external_order_ref == "20-2").one()
        assert full.refund_amount == Decimal("7.44")          # buyer got everything back
        assert order_money.fees(full) == 0                    # eBay returned the whole fee
        assert full.shipping_label_cost == Decimal("1.10")
        assert partial.refund_amount == Decimal("1.10")       # 0.94 net + 0.16 fee credit
        assert order_money.fees(partial) == Decimal("0.84")   # 1.00 - 0.16
        # buyer paid 4.09, got 1.10 back, fee 0.84, label 1.80 -> 0.35 left
        assert order_money.profit(partial) == Decimal("0.35")
        fees = db.query(Expense).filter(Expense.tenant_id == tenant_id).all()
        assert len(fees) == 1 and fees[0].amount == Decimal("0.42") and fees[0].external_ref == "FEE-1"


def test_accounting_counts_labels(client, register):
    headers, _ = register()
    pid = client.post("/products", headers=headers, json={"sku": "L1", "title": "L1", "purchase_price": 1, "selling_price": 3, "stock_quantity": 5}).json()["id"]
    oid = client.post("/orders", headers=headers, json={"items": [{"product_id": pid, "quantity": 1}]}).json()["id"]
    with SessionLocal() as db:
        db.query(Order).filter(Order.id == uuid.UUID(oid)).update({"shipping_label_cost": Decimal("1.20")})
        db.commit()
    t = client.get("/accounting/summary", headers=headers, params={"start": "2020-01-01", "end": "2029-12-31"}).json()["totals"]
    assert t["shipping_labels"] == "1.20"
    assert float(t["net_profit"]) == 3 - 1 - 1.2
