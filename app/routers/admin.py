import re
import secrets
import uuid
from datetime import datetime, timezone

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
from app.models.site_page import SitePage
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
from app.schemas.site_page import SitePageOut, SitePageUpdate
from app.routers.affiliate_portal import _issue_set_password_token
from app.routers.plans import DEFAULT_SITE_PAGES, _get_or_create_page
from app.services.billing_service import issue_invoice
from app.services.email_service import send_affiliate_password_email, send_invoice_email
from app.services.invoice_service import generate_invoice_pdf

router = APIRouter(prefix="/admin", tags=["admin"])

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
    19% USt) as a PDF, and emails it to the tenant's account owner - via the
    exact same app/services/billing_service.py path a real Stripe payment
    triggers automatically (see app/routers/billing.py's webhook). This is
    the manual fallback for payment methods Stripe doesn't handle (bank
    transfer, PayPal, ...).
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

    try:
        invoice = issue_invoice(db, tenant, owner, settings_row, net_amount, description, currency)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    except RuntimeError as err:
        # The invoice itself is already committed (its number must never be
        # reused, and the message names it) - only the email failed, so say
        # so distinctly rather than a generic 500; the admin can retry via
        # the resend endpoint.
        raise HTTPException(status_code=502, detail=str(err))

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
    db.flush()  # assigns affiliate.id, needed for the token below

    if affiliate.email:
        raw_token = _issue_set_password_token(db, affiliate)

    db.commit()
    db.refresh(affiliate)

    if affiliate.email:
        set_password_link = f"{settings.frontend_url}/affiliate-set-password.html?token={raw_token}"
        try:
            send_affiliate_password_email(affiliate.email, set_password_link, is_initial_setup=True)
        except RuntimeError:
            pass  # affiliate is still created - admin can resend/reset the link later

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


@router.post("/affiliates/{affiliate_id}/resend-password-setup")
def resend_affiliate_password_setup(
    affiliate_id: str,
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    """
    Re-sends the "set your password" email - for an affiliate who never got
    or lost the original one. Also works for an affiliate who already has a
    password (the email just reads as a reset link instead), since there's
    no other admin-facing way to get them a fresh link once the first one
    has expired or gone missing.
    """
    affiliate = db.query(Affiliate).filter(Affiliate.id == affiliate_id).first()
    if not affiliate:
        raise HTTPException(status_code=404, detail="Affiliate nicht gefunden")
    if not affiliate.email:
        raise HTTPException(status_code=400, detail="Dieser Affiliate hat keine E-Mail-Adresse hinterlegt.")

    is_initial_setup = affiliate.password_hash is None
    raw_token = _issue_set_password_token(db, affiliate)
    db.commit()

    set_password_link = f"{settings.frontend_url}/affiliate-set-password.html?token={raw_token}"
    try:
        send_affiliate_password_email(affiliate.email, set_password_link, is_initial_setup=is_initial_setup)
    except RuntimeError as err:
        raise HTTPException(status_code=502, detail=f"E-Mail-Versand fehlgeschlagen: {err}")

    return {"message": "Link wurde erneut gesendet."}


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


@router.get("/pages", response_model=list[SitePageOut])
def list_site_pages(
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    """The fixed set of public content pages (see DEFAULT_SITE_PAGES in
    app/routers/plans.py) - creates any that don't exist yet with placeholder
    content, so this always returns all of them."""
    return [_get_or_create_page(db, slug) for slug in DEFAULT_SITE_PAGES]


@router.patch("/pages/{slug}", response_model=SitePageOut)
def update_site_page(
    slug: str,
    payload: SitePageUpdate,
    current_user: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    page = _get_or_create_page(db, slug)  # 404s if slug isn't one of the known pages

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(page, field, value)

    db.commit()
    db.refresh(page)
    return page
