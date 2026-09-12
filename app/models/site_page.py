"""
Simple admin-editable content pages ("Über uns", "Richtlinien", "Kontakt")
shown publicly on the marketing site. Deliberately NOT a general-purpose CMS
with arbitrary slugs - see DEFAULT_SITE_PAGES in app/routers/plans.py for
the fixed, known set this supports. content is plain text (paragraphs
separated by a blank line, rendered/escaped on the frontend) rather than
HTML, so there's no stored-XSS surface even though only the trusted
Super Admin can ever write to it.
"""

import uuid

from sqlalchemy import Column, DateTime, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base


class SitePage(Base):
    __tablename__ = "site_pages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug = Column(String(50), nullable=False, unique=True)
    title = Column(String(255), nullable=False)
    content = Column(Text, nullable=False)

    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
