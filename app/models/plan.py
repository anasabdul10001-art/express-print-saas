"""
Subscription plans, managed by the Super Admin. Deliberately has no
enforcement logic yet and no payment fields wired up to anything real -
this is the content/configuration side (what a plan is called, costs, and
includes) built ahead of the actual Stripe checkout integration, which is
being done as a separate, later piece of work.
"""

import uuid

from sqlalchemy import Boolean, Column, DateTime, DECIMAL, Integer, String
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.sql import func

from app.database import Base


class Plan(Base):
    __tablename__ = "plans"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), nullable=False)
    description = Column(String(500))

    price_monthly = Column(DECIMAL(10, 2))
    currency = Column(String(3), nullable=False, default="EUR")

    # Null = unlimited. Not enforced anywhere yet - see module docstring.
    order_limit = Column(Integer, nullable=True)
    printer_limit = Column(Integer, nullable=True)
    user_limit = Column(Integer, nullable=True)

    # Null or 0 = no trial. Not enforced anywhere yet - see module docstring.
    trial_days = Column(Integer, nullable=True)

    features = Column(ARRAY(String), nullable=False, default=list)

    is_active = Column(Boolean, nullable=False, default=True)  # shown on the (future) public pricing page
    is_recommended = Column(Boolean, nullable=False, default=False)
    display_order = Column(Integer, nullable=False, default=0)

    # Filled in once the Stripe integration exists. Null until then.
    stripe_price_id_monthly = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SiteSettings(Base):
    """
    Single-row table (one platform, one logo) rather than a key/value store
    - simplest thing that works for the settings that currently exist.
    """

    __tablename__ = "site_settings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    logo_url = Column(String(1000), nullable=True)
    # Rendered height in px on every page that shows the logo (frontend/site-logo.js).
    # Null = default size (32px, same as the icon mark it replaces).
    logo_height = Column(Integer, nullable=True)

    # Seller details printed on every invoice (see app/services/invoice_service.py).
    # Must be filled in before the first invoice can be issued - see the
    # check in routers/admin.py::confirm_payment.
    company_legal_name = Column(String(255), nullable=True)
    company_address = Column(String(1000), nullable=True)  # multi-line, newline-separated
    company_tax_id = Column(String(100), nullable=True)  # Steuernummer or USt-IdNr
    company_email = Column(String(255), nullable=True)

    # Sequential, gapless per calendar year (invoice numbers look like
    # "2026-0007") - required by §14 UStG. Never decremented or reused.
    last_invoice_number = Column(Integer, nullable=False, default=0)

    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
