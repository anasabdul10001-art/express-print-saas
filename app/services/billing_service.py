"""
Shared invoice-issuing logic, used by both the Super Admin's manual
"confirm payment" action (app/routers/admin.py::confirm_payment) and the
Stripe webhook's automatic one (app/routers/billing.py) once a real
subscription payment succeeds - a Stripe-driven payment is a real payment
event exactly like a manually-confirmed bank transfer, so it goes through
the same §14-UStG invoice + affiliate-commission path rather than a
separate one.
"""

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.affiliate import Affiliate, AffiliateCommission, AffiliateReferral
from app.models.invoice import Invoice
from app.models.plan import SiteSettings
from app.models.tenant import Tenant
from app.models.user import User
from app.services.email_service import send_invoice_email
from app.services.invoice_service import generate_invoice_pdf

INVOICE_VAT_RATE = Decimal("19.00")


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
) -> Invoice:
    """
    Creates + commits a sequential, gapless (§14 UStG) invoice row, then
    best-effort awards an affiliate commission and emails the PDF.

    Raises ValueError if the seller's billing details aren't filled in yet -
    the caller decides how to surface that (an HTTP 400 for the admin's
    manual action; logged and skipped for the Stripe webhook, which must
    never fail its response to Stripe over a downstream configuration gap).
    A failure emailing the PDF raises RuntimeError separately - by then the
    invoice itself is already committed and must never be rolled back over
    it; email is retried later via app/routers/admin.py's resend endpoint.
    """
    if not settings_row.company_legal_name or not settings_row.company_address:
        raise ValueError("Bitte zuerst die Rechnungsdaten (Firmenname, Adresse) in den Einstellungen ausfüllen.")

    vat_amount = (net_amount * INVOICE_VAT_RATE / 100).quantize(Decimal("0.01"))
    gross_amount = net_amount + vat_amount

    # Sequential + gapless per §14 UStG: reserved by incrementing the
    # counter in the SAME transaction as creating the invoice row below, so
    # a failed commit rolls back both together rather than burning a number
    # with no invoice behind it.
    settings_row.last_invoice_number = (settings_row.last_invoice_number or 0) + 1
    invoice_number = f"{datetime.now(timezone.utc).year}-{settings_row.last_invoice_number:04d}"

    invoice = Invoice(
        tenant_id=tenant.id,
        invoice_number=invoice_number,
        issue_date=datetime.now(timezone.utc).date(),
        description=description,
        net_amount=net_amount,
        vat_rate=INVOICE_VAT_RATE,
        vat_amount=vat_amount,
        gross_amount=gross_amount,
        currency=currency,
        customer_name=tenant.company_name,
        customer_email=owner.email,
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)

    try:
        award_affiliate_commission(db, tenant, invoice)
    except Exception:  # noqa: BLE001 - the invoice already succeeded and must never roll back over an affiliate bug
        db.rollback()

    pdf_bytes = generate_invoice_pdf(invoice, settings_row)
    try:
        send_invoice_email(owner.email, invoice.invoice_number, pdf_bytes, settings_row.company_legal_name)
    except RuntimeError as err:
        # invoice_number is folded into the message here (rather than left
        # to the caller) because the invoice object itself is otherwise
        # unreachable from a caught exception - the invoice is already
        # committed and must never be rolled back over an email failure.
        raise RuntimeError(f"Rechnung {invoice.invoice_number} wurde erstellt, aber der E-Mail-Versand ist fehlgeschlagen: {err}")

    return invoice
