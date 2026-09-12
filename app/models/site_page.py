"""
Admin-editable static content pages (Impressum, AGB, FAQ, etc.) - one row
per page, addressed by a fixed slug. Unlike Plan, new slugs are defined in
code (see app/content/site_pages.py) and a row is lazily created with
default content the first time anyone requests it (public visit or admin
panel), so a fresh deployment needs no separate seed script.
"""

import uuid

from sqlalchemy import Column, DateTime, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base


class SitePage(Base):
    __tablename__ = "site_pages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug = Column(String(50), unique=True, nullable=False, index=True)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)  # raw HTML, rendered inside a `.prose` container

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
