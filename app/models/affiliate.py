"""
Affiliate/referral program. An affiliate can be an existing ShipSync tenant
referring other sellers, or a completely external marketer/influencer with
no ShipSync account at all (tenant_id stays null) - both share the same
referral_code + commission mechanism, per the product decision to support
either kind under one system.

Commission is only ever created when a tenant's payment is manually
confirmed (see app/routers/admin.py::confirm_payment - there is no
automatic billing yet, same as invoicing itself), keeping this consistent
with how money already moves through this app: nothing here assumes a real
payment gateway exists.

Payouts are tracked (AffiliatePayout) but not automated - marking one paid
is a manual admin action, same spirit as the rest of billing today.
"""

import uuid

from sqlalchemy import Column, DateTime, DECIMAL, ForeignKey, String, Boolean, CheckConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base


class Affiliate(Base):
    __tablename__ = "affiliates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Null when this affiliate has no ShipSync account of their own (a pure
    # external marketer) - set when an existing tenant is also an affiliate.
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True)

    name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=True)  # contact email, especially for affiliates with no tenant_id

    # Null until the affiliate sets a password (via the emailed setup link -
    # see app/routers/affiliate_portal.py). Deliberately separate from
    # User.password_hash even when tenant_id is set: an affiliate's portal
    # login is independent of any ShipSync tenant login they might also have.
    # An affiliate with no email at all can never set one and simply has no
    # self-service portal access - the admin panel remains the only way to
    # manage them.
    password_hash = Column(String, nullable=True)

    referral_code = Column(String(30), nullable=False, unique=True)

    # PERCENTAGE_RECURRING: commission_value is a % (0-100) of every
    # confirmed payment from a referred tenant, for as long as they stay a
    # customer. FLAT_ONE_TIME: commission_value is a flat EUR amount, paid
    # exactly once - the first time a referred tenant's payment is
    # confirmed (see AffiliateReferral.bonus_awarded).
    commission_type = Column(String(20), nullable=False)
    commission_value = Column(DECIMAL(10, 2), nullable=False)

    # Running totals, kept in sync with AffiliateCommission/AffiliatePayout
    # inserts in the same transaction - same "snapshot + ledger" pattern as
    # Product.stock_quantity + StockMovement.
    balance_owed = Column(DECIMAL(10, 2), nullable=False, default=0)
    total_earned = Column(DECIMAL(10, 2), nullable=False, default=0)

    status = Column(String(20), nullable=False, default="ACTIVE")  # ACTIVE / DISABLED

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("commission_type IN ('PERCENTAGE_RECURRING','FLAT_ONE_TIME')", name="ck_affiliates_commission_type"),
        CheckConstraint("status IN ('ACTIVE','DISABLED')", name="ck_affiliates_status"),
    )


class AffiliateReferral(Base):
    __tablename__ = "affiliate_referrals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    affiliate_id = Column(UUID(as_uuid=True), ForeignKey("affiliates.id", ondelete="CASCADE"), nullable=False)
    # unique: a tenant can only ever have been referred by one affiliate -
    # whichever referral_code was used (if any) at signup, permanently.
    referred_tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, unique=True)

    # For FLAT_ONE_TIME affiliates only - flips true the first time the
    # bonus is awarded, so a tenant's second/third/... confirmed payment
    # never pays the flat bonus again. Irrelevant for PERCENTAGE_RECURRING
    # affiliates, who earn on every confirmed payment regardless.
    bonus_awarded = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AffiliateCommission(Base):
    """One ledger row per commission event - the audit trail balance_owed/
    total_earned are computed from, exactly like StockMovement is for
    Product.stock_quantity."""

    __tablename__ = "affiliate_commissions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    affiliate_id = Column(UUID(as_uuid=True), ForeignKey("affiliates.id", ondelete="CASCADE"), nullable=False)
    referral_id = Column(UUID(as_uuid=True), ForeignKey("affiliate_referrals.id", ondelete="CASCADE"), nullable=False)
    invoice_id = Column(UUID(as_uuid=True), ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True)

    amount = Column(DECIMAL(10, 2), nullable=False)
    commission_type = Column(String(20), nullable=False)  # snapshot of the affiliate's type at the time this was earned
    description = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AffiliatePayout(Base):
    """One ledger row per manual payout to an affiliate - reduces
    balance_owed but never total_earned (that stays a lifetime total)."""

    __tablename__ = "affiliate_payouts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    affiliate_id = Column(UUID(as_uuid=True), ForeignKey("affiliates.id", ondelete="CASCADE"), nullable=False)

    amount = Column(DECIMAL(10, 2), nullable=False)
    note = Column(String(500), nullable=True)  # e.g. "Bank transfer 2026-09", a manual reference

    created_at = Column(DateTime(timezone=True), server_default=func.now())
