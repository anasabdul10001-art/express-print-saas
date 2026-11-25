"""
The personal and business details of sections A, B and D of the Anlage EKS, kept
only if the seller asks for it, so they don't have to be typed each time. They are
stored as one encrypted blob and can be deleted at any time.
"""

from sqlalchemy import Column, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base


class EksProfile(Base):
    __tablename__ = "eks_profiles"

    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    data_encrypted = Column(Text, nullable=False)  # JSON of schemas in services/eks_profile_service.py
    consented_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
