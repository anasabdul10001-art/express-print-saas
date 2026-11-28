"""
Shared invoice-issuing logic, used by both the Super Admin's manual
"confirm payment" action (app/routers/admin.py::confirm_payment) and the
Stripe webhook's automatic one (app/routers/billing.py) once a real
subscription payment succeeds - a Stripe-driven payment is a real payment
event exactly like a manually-confirmed bank transfer, so it goes through
the same invoice + email + affiliate-commission path rather than a
separate one.
"""

import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import settings
from app.models.affiliate import Affiliate, AffiliateCommission, AffiliateReferral
from app.models.invoice import Invoice
from app.models.plan import SiteSettings
from app.models.tenant import Tenant
from app.models.user import User
from app.services.email_service import send_invoice_email, send_welcome_email
from app.services.invoice_service import generate_invoice_pdf

INVOICE_VAT_RATE = Decimal("19.00")
# Invoices go to German businesses only for now (see schemas/tenant.py::BillingProfile).
BILLING_COUNTRIES = ("DE",)
BILLING_REQUIRED = ("billing_name", "billing_street", "billing_zip", "billing_city", "billing_country")


def invoice_vat_rate() -> Decimal:
    """A small business (§ 19 UStG) must not show VAT: a wrongly shown amount is owed (§ 14c UStG)."""
    return Decimal("0.00") if settings.operator_small_business else INVOICE_VAT_RATE


def today() -> date:
    return datetime.now(ZoneInfo("Europe/Berlin")).date()


def billing_profile_complete(tenant: Tenant) -> bool:
    return all((getattr(tenant, field) or "").strip() for field in BILLING_REQUIRED) \
        and tenant.billing_country in BILLING_COUNTRIES


def month_period(start: date) -> tuple[date, date]:
    """From start to the day before the same day of the next month."""
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    last = calendar.monthrange(year, month)[1]
    if start.day > last:  # e.g. 31.01.: the period runs to the end of February
        return start, date(year, month, last)
    return start, date(year, month, start.day) - timedelta(days=1)


def subscription_description(plan_name: str | None) -> str:
    plan = f" - Tarif {plan_name}" if plan_name else ""
    return f"NettoDesk Software-Abonnement{plan} (Nutzung der Software / software subscription)"


def require_invoice_data(tenant: Tenant, settings_row: SiteSettings) -> None:
    missing = [label for value, label in (
        (settings_row.company_legal_name, "Firmenname"),
        (settings_row.company_address, "Adresse"),
        (settings_row.company_tax_id, "Steuernummer oder USt-IdNr."),
    ) if not (value or "").strip()]
    if missing:
        raise ValueError("Bitte zuerst die Rechnungsdaten in den Einstellungen ausfüllen: " + ", ".join(missing) + ".")
    if not billing_profile_complete(tenant):
        raise ValueError("Die Rechnungsadresse des Kunden ist unvollständig (nur Anschriften in Deutschland).")


def _lock_settings(db: Session, settings_row: SiteSettings) -> SiteSettings:
    """Row lock until the commit: two payments at once can't get the same number."""
    return (db.query(SiteSettings).filter(SiteSettings.id == settings_row.id)
            .with_for_update().populate_existing().one())


def _next_number(settings_row: SiteSettings, issued_on: date) -> str:
    # Reserved in the same transaction as the invoice row, so a failure
    # rolls both back together instead of burning a number.
    settings_row.last_invoice_number = (settings_row.last_invoice_number or 0) + 1
    return f"{issued_on.year}-{settings_row.last_invoice_number:04d}"


def award_affiliate_commission(db: Session, tenant: Tenant, invoice: Invoice) -> None:
    """
    Called right after a payment is confirmed (invoice already committed).
    Best-effort and isolated in its own try/except at the call site: a bug
    here must never take down billing, which already succeeded by the time
    this runs.

    FLAT_ONE_TIME affiliates are paid exactly once per referred tenant
    (guarded by AffiliateReferral.bonus_awarded); PERCENTAGE_RECURRING
    affiliates earn a cut of every confirmed payment, for as long as the
    referral relationship exists.
    """
    referral = db.query(AffiliateReferral).filter(AffiliateReferral.referred_tenant_id == tenant.id).first()
    if not referral:
        return

    affiliate = db.query(Affiliate).filter(Affiliate.id == referral.affiliate_id).first()
    if not affiliate or affiliate.status != "ACTIVE":
        return

    if affiliate.commission_type == "FLAT_ONE_TIME":
        if referral.bonus_awarded:
            return
        amount = affiliate.commission_value
        description = f"Einmalige Provision für Empfehlung von {tenant.company_name}"
        referral.bonus_awarded = True
    else:  # PERCENTAGE_RECURRING
        amount = (invoice.gross_amount * affiliate.commission_value / 100).quantize(Decimal("0.01"))
        description = f"{affiliate.commission_value}% Provision auf Rechnung {invoice.invoice_number}"

    if amount <= 0:
        return

    db.add(AffiliateCommission(
        affiliate_id=affiliate.id,
        referral_id=referral.id,
        invoice_id=invoice.id,
        amount=amount,
        commission_type=affiliate.commission_type,
        description=description,
    ))
    affiliate.balance_owed = (affiliate.balance_owed or Decimal("0")) + amount
    affiliate.total_earned = (affiliate.total_earned or Decimal("0")) + amount
    db.commit()


def issue_invoice(
    db: Session,
    tenant: Tenant,
    owner: User,
    settings_row: SiteSettings,
    net_amount: Decimal,
    description: str,
    currency: str = "EUR",
    *,
    service_period: tuple[date, date] | None = None,
    payment_method: str = "manual",
    stripe_invoice_id: str | None = None,
    plan_name: str | None = None,
) -> Invoice:
    """
    Creates + commits the invoice with its stored PDF, then (best effort)
    awards an affiliate commission and sends the welcome email (first paid
    subscription only) and the invoice email.

    Raises ValueError if seller or customer details are missing or a text
    can't be printed - nothing is saved and no number is used up.
    Raises RuntimeError if only the invoice email failed: the invoice is
    committed by then and is sent again later (deliver_invoice).
    """
    require_invoice_data(tenant, settings_row)
    issued_on = today()
    start, end = service_period or month_period(issued_on)
    if end < start:
        raise ValueError("Der Leistungszeitraum endet vor seinem Beginn.")
    settings_row = _lock_settings(db, settings_row)

    # Another delivery of the same payment may have won while we waited for the lock.
    if stripe_invoice_id:
        existing = db.query(Invoice).filter(Invoice.stripe_invoice_id == stripe_invoice_id).first()
        if existing:
            db.rollback()
            return existing

    vat_rate = invoice_vat_rate()
    vat_amount = (net_amount * vat_rate / 100).quantize(Decimal("0.01"))
    try:
        invoice = Invoice(
            tenant_id=tenant.id,
            invoice_number=_next_number(settings_row, issued_on),
            issue_date=issued_on,
            description=description[:255],
            net_amount=net_amount,
            vat_rate=vat_rate,
            vat_amount=vat_amount,
            gross_amount=net_amount + vat_amount,
            currency=currency,
            customer_name=tenant.billing_name.strip(),
            customer_email=owner.email,
            customer_street=tenant.billing_street.strip(),
            customer_zip_city=f"{tenant.billing_zip.strip()} {tenant.billing_city.strip()}",
            customer_country=tenant.billing_country,
            customer_vat_id=(tenant.billing_vat_id or "").strip() or None,
            service_start=start,
            service_end=end,
            paid_on=issued_on,
            payment_method=payment_method,
            stripe_invoice_id=stripe_invoice_id,
        )
        invoice.pdf = generate_invoice_pdf(invoice, settings_row)
        db.add(invoice)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(invoice)

    try:
        award_affiliate_commission(db, tenant, invoice)
    except Exception:  # noqa: BLE001 - the invoice already succeeded and must never roll back over an affiliate bug
        db.rollback()

    welcome_once(db, tenant, owner, plan_name, settings_row.company_legal_name)
    deliver_invoice(db, invoice, settings_row)
    return invoice


def welcome_once(db: Session, tenant: Tenant, owner: User, plan_name: str | None, sender_name: str | None) -> None:
    """Welcome email with the first paid subscription. A failure never stops the
    invoice; the next payment (or redelivery) tries again."""
    if tenant.welcome_email_sent_at is not None:
        return
    try:
        send_welcome_email(owner.email, tenant.billing_name or tenant.company_name, plan_name,
                           sender_name, settings.frontend_url)
    except Exception as err:  # noqa: BLE001
        print(f"[billing] welcome email failed for tenant {tenant.id}: {err}")
        return
    tenant.welcome_email_sent_at = datetime.now(timezone.utc)
    db.commit()


def stored_pdf(db: Session, invoice: Invoice, settings_row: SiteSettings) -> bytes:
    """The PDF as issued. Invoices from before PDFs were stored are built once and then kept."""
    if invoice.pdf is None:
        original = db.get(Invoice, invoice.cancels_id) if invoice.cancels_id else None
        invoice.pdf = generate_invoice_pdf(
            invoice, settings_row, original.invoice_number if original else None,
            original.issue_date if original else None)
        db.commit()
    return invoice.pdf


def deliver_invoice(db: Session, invoice: Invoice, settings_row: SiteSettings) -> None:
    """Sends the stored PDF; raises RuntimeError if that fails."""
    try:
        send_invoice_email(invoice.customer_email, invoice.invoice_number, stored_pdf(db, invoice, settings_row),
                           settings_row.company_legal_name, cancellation=invoice.cancels_id is not None)
    except Exception as err:  # noqa: BLE001 - includes network errors from the mail service
        # The number is part of the message: the invoice is committed and the
        # admin needs it to send it again.
        raise RuntimeError(
            f"Rechnung {invoice.invoice_number} wurde erstellt, aber der E-Mail-Versand ist fehlgeschlagen: {err}")
    invoice.email_sent_at = datetime.now(timezone.utc)
    db.commit()


def cancel_invoice(db: Session, original: Invoice, settings_row: SiteSettings) -> Invoice:
    """
    Issues a cancellation invoice with negated amounts; the original stays
    unchanged. It does not refund money and does not reverse an affiliate
    commission - both stay manual.
    """
    if original.cancels_id is not None:
        raise ValueError("Eine Stornorechnung kann nicht storniert werden.")
    if not (settings_row.company_legal_name and settings_row.company_address and settings_row.company_tax_id):
        raise ValueError("Bitte zuerst die Rechnungsdaten in den Einstellungen ausfüllen.")
    settings_row = _lock_settings(db, settings_row)
    if db.query(Invoice.id).filter(Invoice.cancels_id == original.id).first():
        db.rollback()
        raise ValueError("Diese Rechnung ist bereits storniert.")

    issued_on = today()
    try:
        storno = Invoice(
            tenant_id=original.tenant_id,
            invoice_number=_next_number(settings_row, issued_on),
            issue_date=issued_on,
            description=original.description,
            net_amount=-original.net_amount,
            vat_rate=original.vat_rate,
            vat_amount=-original.vat_amount,
            gross_amount=-original.gross_amount,
            currency=original.currency,
            customer_name=original.customer_name,
            customer_email=original.customer_email,
            customer_street=original.customer_street,
            customer_zip_city=original.customer_zip_city,
            customer_country=original.customer_country,
            customer_vat_id=original.customer_vat_id,
            service_start=original.service_start,
            service_end=original.service_end,
            cancels_id=original.id,
        )
        storno.pdf = generate_invoice_pdf(storno, settings_row, original.invoice_number, original.issue_date)
        db.add(storno)
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(storno)
    return storno
