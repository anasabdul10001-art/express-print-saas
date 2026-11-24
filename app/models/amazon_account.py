import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base


class AmazonAccount(Base):
    """
    One row per connected Amazon seller account (a tenant may connect several).
    The long-lived LWA refresh token is stored encrypted (app/core/crypto.py);
    access tokens are short-lived and only kept in memory.
    """

    __tablename__ = "amazon_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)

    seller_id = Column(String(50), nullable=False)  # Amazon "selling_partner_id", e.g. A1B2C3D4E5F6G7
    marketplace_id = Column(String(20), nullable=False, default="A1PA6795UKMFR9")  # Amazon.de
    refresh_token_encrypted = Column(Text, nullable=True)

    # CONNECTED / DISCONNECTED / ERROR
    status = Column(String(20), nullable=False, default="DISCONNECTED")

    connected_at = Column(DateTime(timezone=True), nullable=True)
    last_sync_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AmazonOAuthState(Base):
    """Short-lived CSRF row, one per in-flight "Connect Amazon" click."""

    __tablename__ = "amazon_oauth_states"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    state = Column(String(255), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
