import uuid

from sqlalchemy import Column, String, DateTime, Boolean, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    email = Column(String(255), nullable=False, unique=True, index=True)
    password_hash = Column(String, nullable=False)
    full_name = Column(String(255))
    role = Column(String(20), nullable=False, default="owner")
    is_active = Column(Boolean, nullable=False, default=True)
    # Platform-level flag, separate from `role` (which describes standing
    # within one's own tenant). Grants access to the cross-tenant Super
    # Admin panel - plans, site logo, etc. Not settable via any tenant-
    # facing endpoint.
    is_superadmin = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())
