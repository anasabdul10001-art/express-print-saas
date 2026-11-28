import uuid

from sqlalchemy import Column, String, DateTime, DECIMAL, Integer, CheckConstraint, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_name = Column(String(255), nullable=False)
    country_code = Column(String(2), nullable=False, default="DE")
    status = Column(String(20), nullable=False, default="active")

    plan_id = Column(UUID(as_uuid=True), ForeignKey("plans.id"), nullable=True)
    # Null = no trial deadline (no plan matched at signup, or the plan has
    # no trial_days set). Once past, get_current_user blocks non-superadmin
    # requests until a real subscription exists - see deps.py.
    trial_ends_at = Column(DateTime(timezone=True), nullable=True)

    # Set once this tenant has checked out via Stripe at least once (see
    # app/routers/billing.py) - null for a tenant still on trial or paying
    # by a manual method (bank transfer, PayPal, ...).
    stripe_customer_id = Column(String(255), nullable=True)
    stripe_subscription_id = Column(String(255), nullable=True)
    # Mirrors Stripe's own subscription status strings (active, past_due,
    # canceled, unpaid, ...) - set from webhook events, not enforced
    # anywhere yet (same "not wired to real limits" spirit as Plan's own
    # order/printer/user limits).
    subscription_status = Column(String(30), nullable=True)
    # End of the billing period that has been paid for, as reported by Stripe's
    # webhooks. Null without a Stripe subscription.
    current_period_end = Column(DateTime(timezone=True), nullable=True)

    # Controls how shipping labels get printed for this tenant's orders:
    #   INSTANT           - auto-print the moment an order arrives
    #   DASHBOARD_MANUAL   - order sits in a list; user clicks "Print" per order
    #   INTEGRATED          - one PDF combining the shipping label + a picking
    #                          slip (product photo, name, shelf location)
    print_mode = Column(String(20), nullable=False, default="DASHBOARD_MANUAL")

    # Paper in the seller's label printer; DHL labels are fitted to it (see label_paper_service).
    label_paper = Column(String(10), nullable=False, default="100x150", server_default="100x150")

    # Fallback package size/weight used to create a DHL shipment when an
    # order's product(s) don't have their own weight_kg/length_cm/etc. set
    # (see Product model) - keeps label creation from ever being blocked on
    # missing package data. A small padded-envelope-ish default.
    default_package_weight_kg = Column(DECIMAL(6, 3), nullable=False, default=1.0)
    default_package_length_cm = Column(Integer, nullable=False, default=20)
    default_package_width_cm = Column(Integer, nullable=False, default=15)
    default_package_height_cm = Column(Integer, nullable=False, default=10)

    # Accounting VAT mode: 'kleinunternehmer' (no VAT, par. 19 UStG) or 'regular' (19%).
    vat_mode = Column(String(20), nullable=False, default="kleinunternehmer", server_default="kleinunternehmer")

    # After how many months buyer names and addresses are removed from finished orders.
    buyer_data_retention_months = Column(Integer, nullable=False, default=36, server_default="36")

    # Recipient on the subscription invoices: full name and address.
    billing_name = Column(String(255), nullable=True)
    billing_street = Column(String(255), nullable=True)
    billing_zip = Column(String(20), nullable=True)
    billing_city = Column(String(100), nullable=True)
    billing_country = Column(String(2), nullable=True)
    billing_vat_id = Column(String(30), nullable=True)
    # Set once the welcome email for the first paid subscription has gone out.
    welcome_email_sent_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "print_mode IN ('INSTANT','DASHBOARD_MANUAL','INTEGRATED')",
            name="ck_tenants_print_mode",
        ),
    )

