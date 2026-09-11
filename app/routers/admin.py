import re
import secrets
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import get_current_superadmin, get_db
from app.models.affiliate import Affiliate, AffiliateCommission, AffiliatePayout, AffiliateReferral
from app.models.invoice import Invoice
from app.models.payment_method import PaymentMethod
from app.models.plan import Plan, SiteSettings
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.admin import (
    ConfirmPaymentRequest,
    InvoiceOut,
    PaymentMethodCreate,
    PaymentMethodOut,
    PaymentMethodUpdate,
    PlanCreate,
    PlanOut,
    PlanUpdate,
    SiteSettingsOut,
    SiteSettingsUpdate,
    TenantAdminOut,
)
from app.schemas.affiliate import (
    AffiliateCommissionOut,
    AffiliateCreate,
    AffiliateOut,
    AffiliatePayoutCreate,
    AffiliatePayoutOut,
    AffiliateUpdate,
)
from app.services.email_service import send_invoice_email
from app.services.invoice_service import generate_invoice_pdf

router = APIRouter(prefix="/admin", tags=["admin"])

INVOICE_VAT_RATE = Decimal("19.00")

# Reuses the same Supabase bucket product images already go into (see
# app/routers/uploads.py) rather than requiring a second bucket to be
# created in the Supabase dashboard just for one logo file.
LOGO_BUCKET = "product-images"
ALLOWED_LOGO_TYPES = {"image/jpeg", "image/png", "image/webp", "image/svg+xml"}


@router.get("/plans", response_model=list[PlanOut])
def list_all_plans(
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    """Includes inactive plans too - unlike GET /plans, which is what the public pricing page uses."""
    return db.query(Plan).order_by(Plan.display_order.asc(), Plan.created_at.asc()).all()


@router.post("/plans", response_model=PlanOut, status_code=201)
def create_plan(
    payload: PlanCreate,
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    plan = Plan(**payload.model_dump())
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


@router.patch("/plans/{plan_id}", response_model=PlanOut)
def update_plan(
    plan_id: str,
    payload: PlanUpdate,
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    plan = db.query(Plan).filter(Plan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan nicht gefunden")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(plan, field, value)

    db.commit()
    db.refresh(plan)
    return plan


@router.delete("/plans/{plan_id}", status_code=204)
def delete_plan(
    plan_id: str,
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    plan = db.query(Plan).filter(Plan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan nicht gefunden")
    db.delete(plan)
    db.commit()


def _get_or_create_settings(db: Session) -> SiteSettings:
    settings_row = db.query(SiteSettings).first()
    if not settings_row:
        settings_row = SiteSettings()
        db.add(settings_row)
        db.commit()
        db.refresh(settings_row)
    return settings_row


def _generate_referral_code(db: Session, name: str) -> str:
    """
    A short, readable, URL-friendly code (e.g. "MAXMUSTER4f2a") rather than
    a random unguessable token - referral codes are meant to be shared
    publicly in links, not kept secret. Retries on the (very unlikely)
    chance of a collision.
    """
    base = re.sub(r"[^A-Z0-9]", "", name.upper())[:12] or "AFFILIATE"
    for _ in range(10):
        code = f"{base}{secrets.token_hex(2)}"
        if not db.query(Affiliate).filter(Affiliate.referral_code == code).first():
            return code
    raise HTTPException(status_code=500, detail="Konnte keinen eindeutigen Referral-Code erzeugen.")


def _award_affiliate_commission(db: Session, tenant: Tenant, invoice: Invoice) -> None:
    """
    Called right after a payment is confirmed (invoice already committed -
    see confirm_payment below). Best-effort and isolated in its own
    try/except at the call site: a bug here must never take down billing,
    which already succeeded by the time this runs.

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


@router.get("/settings", response_model=SiteSettingsOut)
def get_site_settings(
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    return _get_or_create_settings(db)


@router.patch("/settings", response_model=SiteSettingsOut)
def update_site_settings(
    payload: SiteSettingsUpdate,
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    settings_row = _get_or_create_settings(db)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(settings_row, field, value)
    db.commit()
    db.refresh(settings_row)
    return settings_row


@router.post("/logo", response_model=SiteSettingsOut)
async def upload_logo(
    file: UploadFile,
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    if file.content_type not in ALLOWED_LOGO_TYPES:
        raise HTTPException(status_code=400, detail="Nur JPEG, PNG, WEBP oder SVG erlaubt.")

    contents = await file.read()
    if len(contents) > 2 * 1024 * 1024:  # 2 MB - a logo has no reason to be bigger
        raise HTTPException(status_code=400, detail="Datei zu groß (max. 2 MB).")

    extension = file.filename.rsplit(".", 1)[-1] if "." in file.filename else "png"
    # Fixed path (not a random uuid) so re-uploading the logo simply
    # overwrites the previous one instead of accumulating old files.
    path = f"site/logo.{extension}"

    upload_url = f"{settings.supabase_url}/storage/v1/object/{LOGO_BUCKET}/{path}"

    async with httpx.AsyncClient() as client:
        response = await client.post(
            upload_url,
            content=contents,
            headers={
                "Authorization": f"Bearer {settings.supabase_service_key}",
                "apikey": settings.supabase_service_key,
                "Content-Type": file.content_type,
                "x-upsert": "true",  # overwrite if a logo was already uploaded at this path
            },
        )

    if response.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail="Logo-Upload fehlgeschlagen.")

    # Cache-bust: without a changing query string, browsers/CDNs would keep
    # showing the old logo image at the same URL after a re-upload.
    public_url = f"{settings.supabase_url}/storage/v1/object/public/{LOGO_BUCKET}/{path}?v={uuid.uuid4().hex[:8]}"

    settings_row = _get_or_create_settings(db)
    settings_row.logo_url = public_url
    db.commit()
    db.refresh(settings_row)
    return settings_row


@router.get("/payment-methods", response_model=list[PaymentMethodOut])
def list_all_payment_methods(
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    """Includes inactive methods too - unlike GET /payment-methods, which is what the public pricing page uses."""
    return db.query(PaymentMethod).order_by(PaymentMethod.display_order.asc(), PaymentMethod.created_at.asc()).all()


@router.post("/payment-methods", response_model=PaymentMethodOut, status_code=201)
def create_payment_method(
    payload: PaymentMethodCreate,
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    method = PaymentMethod(**payload.model_dump())
    db.add(method)
    db.commit()
    db.refresh(method)
    return method


@router.patch("/payment-methods/{method_id}", response_model=PaymentMethodOut)
def update_payment_method(
    method_id: str,
    payload: PaymentMethodUpdate,
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    method = db.query(PaymentMethod).filter(PaymentMethod.id == method_id).first()
    if not method:
        raise HTTPException(status_code=404, detail="Zahlungsart nicht gefunden")

    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(method, field, value)

    db.commit()
    db.refresh(method)
    return method


@router.delete("/payment-methods/{method_id}", status_code=204)
def delete_payment_method(
    method_id: str,
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    method = db.query(PaymentMethod).filter(PaymentMethod.id == method_id).first()
    if not method:
        raise HTTPException(status_code=404, detail="Zahlungsart nicht gefunden")
    db.delete(method)
    db.commit()


@router.get("/tenants", response_model=list[TenantAdminOut])
def list_all_tenants(
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    """
    Feeds the "confirm payment" picker in admin.html. Owner email and plan
    name/price are looked up per tenant rather than joined - this list is
    small (Super Admin only, not paginated anywhere in the UI yet) and the
    extra queries keep this readable; revisit if the tenant count ever
    makes that noticeable.
    """
    tenants = db.query(Tenant).order_by(Tenant.created_at.desc()).all()
    result = []
    for tenant in tenants:
        owner = (
            db.query(User)
            .filter(User.tenant_id == tenant.id)
            .order_by(User.created_at.asc())
            .first()
        )
        plan = db.query(Plan).filter(Plan.id == tenant.plan_id).first() if tenant.plan_id else None
        result.append(TenantAdminOut(
            id=tenant.id,
            company_name=tenant.company_name,
            country_code=tenant.country_code,
            status=tenant.status,
            plan_id=tenant.plan_id,
            plan_name=plan.name if plan else None,
            plan_price=plan.price_monthly if plan else None,
            plan_currency=plan.currency if plan else None,
            owner_email=owner.email if owner else None,
            created_at=tenant.created_at,
        ))
    return result


@router.post("/tenants/{tenant_id}/confirm-payment", response_model=InvoiceOut, status_code=201)
def confirm_payment(
    tenant_id: str,
    payload: ConfirmPaymentRequest,
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    """
    Manually confirming that a tenant's payment (bank transfer, PayPal, ...
    - see the Payment Methods section) has arrived. Issues a German
    §14-UStG-compliant invoice (sequential number, seller/customer details,
    19% USt) as a PDF, and emails it to the tenant's account owner - this
    is the ONLY way an invoice gets created; there is no automatic billing
    yet (see app/models/plan.py's module docstring).
    """
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant nicht gefunden")

    owner = (
        db.query(User)
        .filter(User.tenant_id == tenant.id)
        .order_by(User.created_at.asc())
        .first()
    )
    if not owner:
        raise HTTPException(status_code=400, detail="Kein Benutzer für diesen Tenant gefunden.")

    plan = db.query(Plan).filter(Plan.id == tenant.plan_id).first() if tenant.plan_id else None

    net_amount = payload.amount if payload.amount is not None else (plan.price_monthly if plan else None)
    if net_amount is None:
        raise HTTPException(status_code=400, detail="Kein Betrag verfügbar - bitte einen Betrag angeben.")

    description = payload.description or (plan.name if plan else "Abonnement")
    currency = plan.currency if plan else "EUR"

    settings_row = _get_or_create_settings(db)
    if not settings_row.company_legal_name or not settings_row.company_address:
        raise HTTPException(
            status_code=400,
            detail="Bitte zuerst die Rechnungsdaten (Firmenname, Adresse) in den Einstellungen ausfüllen.",
        )

    vat_amount = (net_amount * INVOICE_VAT_RATE / 100).quantize(Decimal("0.01"))
    gross_amount = net_amount + vat_amount

    # Sequential + gapless per §14 UStG: reserved by incrementing the
    # counter in the SAME transaction as creating the invoice row below,
    # so a failed commit rolls back both together rather than burning a
    # number with no invoice behind it.
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
        _award_affiliate_commission(db, tenant, invoice)
    except Exception:  # noqa: BLE001 - the invoice already succeeded and must never roll back over an affiliate bug
        db.rollback()

    pdf_bytes = generate_invoice_pdf(invoice, settings_row)

    try:
        send_invoice_email(owner.email, invoice.invoice_number, pdf_bytes, settings_row.company_legal_name)
    except RuntimeError as err:
        # The invoice itself is already committed (its number must never be
        # reused) - only the email failed, so say so distinctly rather than
        # a generic 500, and let the admin retry via the resend endpoint.
        raise HTTPException(
            status_code=502,
            detail=f"Rechnung {invoice.invoice_number} wurde erstellt, aber der E-Mail-Versand ist fehlgeschlagen: {err}",
        )

    return invoice


@router.get("/invoices", response_model=list[InvoiceOut])
def list_invoices(
    tenant_id: str | None = None,
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    query = db.query(Invoice)
    if tenant_id:
        query = query.filter(Invoice.tenant_id == tenant_id)
    return query.order_by(Invoice.issue_date.desc(), Invoice.invoice_number.desc()).all()


@router.get("/invoices/{invoice_id}/pdf")
def download_invoice_pdf(
    invoice_id: str,
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    """
    Regenerates the PDF on demand from the stored invoice + current seller
    settings, rather than persisting the file anywhere - an invoice PDF
    contains a customer's name, email and billing amounts, so it never gets
    a public URL the way product images/logos/avatars do.
    """
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Rechnung nicht gefunden")

    settings_row = _get_or_create_settings(db)
    pdf_bytes = generate_invoice_pdf(invoice, settings_row)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="Rechnung-{invoice.invoice_number}.pdf"'},
    )


@router.post("/invoices/{invoice_id}/resend")
def resend_invoice(
    invoice_id: str,
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Rechnung nicht gefunden")

    settings_row = _get_or_create_settings(db)
    pdf_bytes = generate_invoice_pdf(invoice, settings_row)

    try:
        send_invoice_email(invoice.customer_email, invoice.invoice_number, pdf_bytes, settings_row.company_legal_name)
    except RuntimeError as err:
        raise HTTPException(status_code=502, detail=f"E-Mail-Versand fehlgeschlagen: {err}")

    return {"message": "Rechnung erneut gesendet."}


def _with_referral_count(db: Session, affiliate: Affiliate) -> AffiliateOut:
    out = AffiliateOut.model_validate(affiliate)
    out.referral_count = (
        db.query(func.count(AffiliateReferral.id))
        .filter(AffiliateReferral.affiliate_id == affiliate.id)
        .scalar()
    ) or 0
    return out


@router.post("/affiliates", response_model=AffiliateOut, status_code=201)
def create_affiliate(
    payload: AffiliateCreate,
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    if payload.tenant_id:
        tenant = db.query(Tenant).filter(Tenant.id == payload.tenant_id).first()
        if not tenant:
            raise HTTPException(status_code=404, detail="Tenant nicht gefunden")

    affiliate = Affiliate(
        tenant_id=payload.tenant_id,
        name=payload.name,
        email=payload.email,
        referral_code=_generate_referral_code(db, payload.name),
        commission_type=payload.commission_type,
        commission_value=payload.commission_value,
    )
    db.add(affiliate)
    db.commit()
    db.refresh(affiliate)
    return _with_referral_count(db, affiliate)


@router.get("/affiliates", response_model=list[AffiliateOut])
def list_affiliates(
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    affiliates = db.query(Affiliate).order_by(Affiliate.created_at.desc()).all()
    return [_with_referral_count(db, a) for a in affiliates]


@router.patch("/affiliates/{affiliate_id}", response_model=AffiliateOut)
def update_affiliate(
    affiliate_id: str,
    payload: AffiliateUpdate,
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    affiliate = db.query(Affiliate).filter(Affiliate.id == affiliate_id).first()
    if not affiliate:
        raise HTTPException(status_code=404, detail="Affiliate nicht gefunden")

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(affiliate, field, value)

    db.commit()
    db.refresh(affiliate)
    return _with_referral_count(db, affiliate)


@router.get("/affiliates/{affiliate_id}/commissions", response_model=list[AffiliateCommissionOut])
def list_affiliate_commissions(
    affiliate_id: str,
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    affiliate = db.query(Affiliate).filter(Affiliate.id == affiliate_id).first()
    if not affiliate:
        raise HTTPException(status_code=404, detail="Affiliate nicht gefunden")

    return (
        db.query(AffiliateCommission)
        .filter(AffiliateCommission.affiliate_id == affiliate_id)
        .order_by(AffiliateCommission.created_at.desc())
        .all()
    )


@router.post("/affiliates/{affiliate_id}/payout", response_model=AffiliateOut)
def record_affiliate_payout(
    affiliate_id: str,
    payload: AffiliatePayoutCreate,
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    """
    Records a manual payout (bank transfer, PayPal, ...) and reduces the
    balance owed - there's no automated payout yet, same as every other
    piece of billing in this app (see confirm_payment above). total_earned
    is a lifetime figure and is deliberately NOT reduced here.
    """
    affiliate = db.query(Affiliate).filter(Affiliate.id == affiliate_id).first()
    if not affiliate:
        raise HTTPException(status_code=404, detail="Affiliate nicht gefunden")

    if payload.amount > affiliate.balance_owed:
        raise HTTPException(
            status_code=400,
            detail=f"Betrag ({payload.amount} €) übersteigt den offenen Saldo ({affiliate.balance_owed} €).",
        )

    db.add(AffiliatePayout(affiliate_id=affiliate.id, amount=payload.amount, note=payload.note))
    affiliate.balance_owed -= payload.amount
    db.commit()
    db.refresh(affiliate)
    return _with_referral_count(db, affiliate)
