"""What a supplier calls a product: learned when a purchase is booked, so the same
item is recognised on the supplier's next invoice."""

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base


class SupplierItem(Base):
    __tablename__ = "supplier_items"
    __table_args__ = (UniqueConstraint("tenant_id", "supplier_norm", "match_value", name="uq_supplier_items"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)

    supplier = Column(String(255), nullable=False)       # as typed, for display
    supplier_norm = Column(String(255), nullable=False)
    match_value = Column(String(520), nullable=False)    # "code:..." or, without a code, "name:..."
    item_name = Column(String(500), nullable=False, default="")
    item_code = Column(String(100), nullable=False, default="")

    product_id = Column(UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    units_per_pack = Column(Integer, nullable=False, default=1)  # shop pieces in one invoiced unit
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
