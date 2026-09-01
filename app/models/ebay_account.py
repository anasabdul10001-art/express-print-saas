import uuid

from sqlalchemy import Column, String, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base


class EbayAccount(Base):
    """
    One row per tenant's connected eBay seller account. Tokens are stored
    encrypted (see app/core/crypto.py) - never in plaintext, never logged.
    """

    __tablename__ = "ebay_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,  # one eBay connection per tenant for now
    )

    ebay_user_id = Column(String(255), nullable=True)
    access_token_encrypted = Column(Text, nullable=True)
    refresh_token_encrypted = Column(Text, nullable=True)
    access_token_expires_at = Column(DateTime(timezone=True), nullable=True)
    refresh_token_expires_at = Column(DateTime(timezone=True), nullable=True)

    # CONNECTED / DISCONNECTED / ERROR
    status = Column(String(20), nullable=False, default="DISCONNECTED")
    # SANDBOX / PRODUCTION - which eBay environment these tokens belong to
    environment = Column(String(20), nullable=False, default="SANDBOX")

    connected_at = Column(DateTime(timezone=True), nullable=True)
    last_sync_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


class EbayOAuthState(Base):
    """
    Short-lived CSRF-protection row: one per in-flight "Connect eBay" click.
    Stored in the DB (not in-memory) because Render can run multiple worker
    processes - a state generated on worker A must still be verifiable when
    eBay's redirect lands on worker B. Deleted immediately after use (or
    once expired), so this table should stay tiny.
    """

    __tablename__ = "ebay_oauth_states"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    state = Column(String(255), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
