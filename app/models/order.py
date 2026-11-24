import uuid

from sqlalchemy import Column, String, DateTime, Integer, DECIMAL, ForeignKey, CheckConstraint, Boolean, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from app.database import Base


class Order(Base):
    __tablename__ = "orders"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)

    external_order_ref = Column(String(100))  # e.g. eBay order ID, once that integration exists
    status = Column(String(20), nullable=False, default="new")

    # The eBay buyer behind an imported order. It's how an account-deletion
    # notification from eBay finds this buyer's data (see
    # app/services/ebay_deletion_service.py).
    buyer_username = Column(String(255), index=True)

    # Recipient shipping address - needed to actually create a DHL shipment
    # label later (see app/routers/dhl.py). Null for orders created manually
    # in the dashboard (no address to capture); populated from eBay's
    # Fulfillment API shipTo data for orders imported via ebay_order_service.
    recipient_name = Column(String(255))
    recipient_street1 = Column(String(255))
    recipient_street2 = Column(String(255))
    recipient_city = Column(String(100))
    recipient_state = Column(String(100))
    recipient_zip = Column(String(20))
    recipient_country_code = Column(String(2))
    recipient_phone = Column(String(50))

    # Set once a DHL shipment is actually created for this order (see
    # app/services/dhl_service.py) - null until then, and for orders never
    # shipped through the DHL integration at all.
    tracking_number = Column(String(50), nullable=True)

    # Total marketplace/payment fees for this sale (eBay Finances API, or the
    # Shopify Payments fees on the order's transactions); null until known.
    ebay_fees = Column(DECIMAL(10, 2), nullable=True)

    # Returns / refunds. refund_amount is what was paid back to the buyer
    # (taken from eBay's payment summary on sync, or edited by hand). By
    # default the goods are treated as LOST (the buyer keeps them), so their
    # cost still counts; item_returned_to_stock=True means the item came back
    # and is resellable, so its cost is not a loss. return_shipping_cost is
    # what the seller paid to get it back.
    refund_amount = Column(DECIMAL(10, 2), nullable=True)
    item_returned_to_stock = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    return_shipping_cost = Column(DECIMAL(10, 2), nullable=True)

    # Which connected eBay account this order came from (null for manual orders).
    ebay_account_id = Column(UUID(as_uuid=True), ForeignKey("ebay_accounts.id", ondelete="SET NULL"), nullable=True)

    # Which connected Shopify store this order came from (null otherwise).
    shopify_account_id = Column(UUID(as_uuid=True), ForeignKey("shopify_accounts.id", ondelete="SET NULL"), nullable=True)

    # Which connected Amazon seller account this order came from (null otherwise).
    amazon_account_id = Column(UUID(as_uuid=True), ForeignKey("amazon_accounts.id", ondelete="SET NULL"), nullable=True)

    # When the sale happened at eBay (created_at is only when we imported it).
    ordered_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "status IN ('new','ready_to_print','printed','shipped','canceled')",
            name="ck_orders_status",
        ),
    )

    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")
    ebay_account = relationship("EbayAccount", foreign_keys=[ebay_account_id])
    shopify_account = relationship("ShopifyAccount", foreign_keys=[shopify_account_id])
    amazon_account = relationship("AmazonAccount", foreign_keys=[amazon_account_id])


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    product_id = Column(UUID(as_uuid=True), ForeignKey("products.id"), nullable=True)

    quantity = Column(Integer, nullable=False, default=1)
    unit_price = Column(DECIMAL(10, 2))

    # Snapshot of the product's purchase_price at order time, so profit
    # stays accurate even if the product's cost later changes (e.g. a new
    # purchase batch at a different price). Same reasoning as shelf_location
    # below: history should reflect what was true when the order happened.
    purchase_price = Column(DECIMAL(10, 2))

    # A shelf_location SNAPSHOT, captured at order-import time. This exists
    # as a fallback/audit trail (e.g. if a product is later deleted or
    # re-SKU'd, this order still shows where it was picked from). It is
    # deliberately NOT the primary source for what a picker sees on screen -
    # see the note in schemas/order.py: the dashboard/label always prefers
    # the PRODUCT's current shelf_location, since that reflects where the
    # item actually is right now. If stock gets relocated after the order
    # comes in but before it's picked, showing the live location prevents
    # sending someone to an empty shelf.
    shelf_location = Column(String(50))

    order = relationship("Order", back_populates="items")
    product = relationship("Product")