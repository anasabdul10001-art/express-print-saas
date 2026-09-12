"""
Single-use token for an affiliate to set their portal password - used both
for the initial "set your password" link (sent when an admin creates an
affiliate with an email) and for a later "forgot password" reset, since
both are the exact same action from the affiliate's point of view. Only the
SHA-256 hash is stored, same pattern as PasswordResetToken for regular users.
"""

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base


class AffiliatePasswordToken(Base):
    __tablename__ = "affiliate_password_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    affiliate_id = Column(UUID(as_uuid=True), ForeignKey("affiliates.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
