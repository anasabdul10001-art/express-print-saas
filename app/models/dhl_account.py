"""
One tenant's own DHL Business Customer (Geschäftskundenversand) account,
connected the same way each tenant connects their own eBay account (see
app/models/ebay_account.py) - each seller ships (and pays DHL) under their
own contract, ShipSync never becomes a shipping cost intermediary.

Unlike eBay, this isn't an OAuth redirect flow: DHL's Parcel DE Shipping
API authenticates with the account's own username/password (Basic Auth)
plus ShipSync's own app-level API key (see app/config.py's dhl_api_key) -
so the tenant types their DHL login in directly, and it's encrypted at
rest exactly like eBay's tokens are.
"""

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base


class DhlAccount(Base):
    __tablename__ = "dhl_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,  # one DHL connection per tenant for now
    )

    # EKP + Verfahren + Teilnahme, e.g. "33333333330101" - identifies which
    # DHL shipping contract to bill each label to.
    billing_number = Column(String(20), nullable=True)
    api_username_encrypted = Column(Text, nullable=True)
    api_password_encrypted = Column(Text, nullable=True)

    # The shipper/sender address printed on every label - the tenant's own
    # warehouse/return address, not the customer's.
    sender_name = Column(String(255), nullable=True)
    sender_street = Column(String(255), nullable=True)
    sender_zip = Column(String(20), nullable=True)
    sender_city = Column(String(100), nullable=True)
    sender_country = Column(String(2), nullable=False, default="DE")

    # CONNECTED / DISCONNECTED / ERROR
    status = Column(String(20), nullable=False, default="DISCONNECTED")
    # SANDBOX / PRODUCTION - which DHL environment these credentials are for.
    environment = Column(String(20), nullable=False, default="SANDBOX")

    connected_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
