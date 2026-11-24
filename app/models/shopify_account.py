import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base


class ShopifyAccount(Base):
    """
    One row per connected Shopify store (a tenant may connect several). The
    offline access token is stored encrypted (app/core/crypto.py), never in
    plaintext and never logged.
    """

    __tablename__ = "shopify_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)

    # e.g. "my-store.myshopify.com" - the store's permanent myshopify domain.
    shop_domain = Column(String(255), nullable=False)
    shop_name = Column(String(255), nullable=True)
    access_token_encrypted = Column(Text, nullable=True)
    scopes = Column(Text, nullable=True)  # what the merchant actually granted

    # CONNECTED / DISCONNECTED / ERROR
    status = Column(String(20), nullable=False, default="DISCONNECTED")

    connected_at = Column(DateTime(timezone=True), nullable=True)
    last_sync_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class ShopifyOAuthState(Base):
    """Short-lived CSRF row, one per in-flight "Connect Shopify" click (see
    EbayOAuthState for why it lives in the database)."""

    __tablename__ = "shopify_oauth_states"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    shop_domain = Column(String(255), nullable=False)
    state = Column(String(255), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
