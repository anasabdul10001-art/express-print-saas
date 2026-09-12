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
    avatar_url = Column(String)
    role = Column(String(20), nullable=False, default="owner")
    is_active = Column(Boolean, nullable=False, default=True)
    # Platform-level flag, separate from `role` (which describes standing
    # within one's own tenant). Grants access to the cross-tenant Super
    # Admin panel - plans, site logo, etc. Not settable via any tenant-
    # facing endpoint.
    is_superadmin = Column(Boolean, nullable=False, default=False)
    # Timestamp of the customer's explicit consent, given at registration, that
    # the service starts immediately and that they therefore lose the statutory
    # 14-day withdrawal right once the service is fully performed (§ 356 Abs. 4
    # BGB - see the "withdrawal" SitePage in app/routers/plans.py). Null would
    # only happen for accounts created before this consent was required.
    early_service_consent_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())
