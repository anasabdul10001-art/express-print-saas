import uuid

from sqlalchemy import Boolean, Column, DateTime, DECIMAL, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base


class Product(Base):
    __tablename__ = "products"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)

    sku = Column(String(100), nullable=False)
    title = Column(String(500), nullable=False)

    # Source of truth for both fields. OrderItem below stores its own
    # shelf_location SNAPSHOT (see note there) but does NOT duplicate
    # image_url - product photos rarely change and a join is cheap, so
    # there's no real benefit to copying it per order, only staleness risk.
    image_url = Column(String(1000))
    shelf_location = Column(String(50))  # e.g. "A3-14", "Regal 2 / Fach C"

    # Phase-1 pricing: two plain numbers, no cost history/valuation method
    # yet (see products roadmap). Enough to show profit-per-unit today;
    # a proper cost-history/COGS engine is a deliberate later phase, not
    # missing-by-accident.
    purchase_price = Column(DECIMAL(10, 2))  # what we pay to get one unit
    selling_price = Column(DECIMAL(10, 2))   # what the customer pays

    active = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())
