"""
Lets a tenant "watch" a search term over time. Since eBay only ever gives
us a live, cumulative estimatedSoldQuantity (never a historical breakdown -
see app/services/ebay_browse_service.py), the only way to get anything
resembling a trend is to record our own daily snapshots and diff them
ourselves, starting from whenever a tenant starts tracking a term. This is
NOT a work-around for eBay's restricted Marketplace Insights API; it is a
fundamentally different, forward-looking-only measurement.
"""

import uuid

from sqlalchemy import Column, Date, DateTime, DECIMAL, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class TrackedSearch(Base):
    __tablename__ = "tracked_searches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    query = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    snapshots = relationship("MarketSnapshot", back_populates="tracked_search", cascade="all, delete-orphan")


class MarketSnapshot(Base):
    """
    One row per (tracked_search, day). `snapshot_date` is a plain date (not
    datetime) specifically so the daily job can upsert-by-uniqueness instead
    of needing separate "did we already run today" bookkeeping.
    """

    __tablename__ = "market_snapshots"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tracked_search_id = Column(UUID(as_uuid=True), ForeignKey("tracked_searches.id", ondelete="CASCADE"), nullable=False)
    snapshot_date = Column(Date, nullable=False)

    item_count = Column(Integer, nullable=False)
    total_estimated_sold = Column(Integer, nullable=True)  # sum across items where eBay reported a number
    average_price = Column(DECIMAL(10, 2), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    tracked_search = relationship("TrackedSearch", back_populates="snapshots")

    __table_args__ = (
        UniqueConstraint("tracked_search_id", "snapshot_date", name="uq_market_snapshot_search_date"),
    )
