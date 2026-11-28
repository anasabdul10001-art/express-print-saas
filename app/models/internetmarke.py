"""
Deutsche Post INTERNETMARKE: one tenant's own Portokasse, and every stamp bought with it.

Each seller pays from their own Portokasse - NettoDesk never handles the money.
"""

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base

# A purchase in one of these states has cost money, or might have.
BLOCKING_STATES = ("PENDING", "UNKNOWN", "BOUGHT")
# Everything about a purchase that leads to the buyer: the label shows name and
# address, the link opens it, the numbers identify the shipment. What was paid stays.
BUYER_DATA = ("label_pdf", "label_link", "voucher_id", "track_id", "error")


class InternetmarkeAccount(Base):
    __tablename__ = "internetmarke_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, unique=True)

    username_encrypted = Column(Text, nullable=False)
    password_encrypted = Column(Text, nullable=False)

    sender_name = Column(String(50), nullable=False)
    sender_street = Column(String(50), nullable=False)
    sender_zip = Column(String(10), nullable=False)
    sender_city = Column(String(35), nullable=False)

    page_format_id = Column(Integer, nullable=True)
    terms_accepted_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class InternetmarkePurchase(Base):
    """
    Written BEFORE the money is spent, so an interrupted purchase stays visible.
    Holds no buyer address as text (the label PDF shows it, like every print job).

    PENDING  checkout sent, no answer stored yet
    UNKNOWN  Deutsche Post may or may not have charged - the seller must check
    BOUGHT   paid
    FAILED   rejected, nothing charged
    """

    __tablename__ = "internetmarke_purchases"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)

    shop_order_id = Column(String(40), nullable=False)
    product_code = Column(Integer, nullable=False)
    price_cents = Column(Integer, nullable=False)
    status = Column(String(10), nullable=False, default="PENDING")

    voucher_id = Column(String(60), nullable=True)
    track_id = Column(String(60), nullable=True)
    label_link = Column(Text, nullable=True)
    label_pdf = Column(LargeBinary, nullable=True)
    error = Column(Text, nullable=True)
    terms_accepted_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # The database itself refuses a second stamp for the same order.
        Index("uq_internetmarke_one_per_order", "order_id", unique=True,
              postgresql_where=text("status IN ('PENDING','UNKNOWN','BOUGHT')")),
    )


def remove_buyer_data(db, *conditions) -> None:
    db.query(InternetmarkePurchase).filter(*conditions).update(
        {getattr(InternetmarkePurchase, name): None for name in BUYER_DATA}, synchronize_session=False)
