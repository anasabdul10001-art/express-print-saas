import uuid

from sqlalchemy import Column, String, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base


class PrintAgent(Base):
    __tablename__ = "print_agents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(100), nullable=False)

    # Only the SHA-256 hash is stored. The raw API key is shown to the user
    # exactly once, at creation time, and is never retrievable again.
    api_key_hash = Column(String(64), nullable=False, unique=True, index=True)

    status = Column(String(20), nullable=False, default="offline")  # online / offline / disabled
    last_seen_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
