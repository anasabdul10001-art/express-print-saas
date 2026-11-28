"""
Stripe subscription billing: Checkout for a tenant to start paying for a
plan, the Customer Portal for them to manage or cancel it, and a webhook
that turns real Stripe payment events into the exact same invoice +
affiliate-commission path a manually confirmed payment already produces
(see app/services/billing_service.py and app/routers/admin.py::confirm_payment).

UNVERIFIED AGAINST REAL STRIPE: built without a live Stripe test-mode key
(none was available at the time - see app/config.py's stripe_secret_key/
stripe_webhook_secret, both empty by default so this whole router is
inert until configured). Checkout Session creation, portal sessions, and
the webhook's signature verification/amount handling have only been
exercised against a mocked `stripe` module in tests/test_billing.py - not
against Stripe's real API. Test end-to-end against a Stripe test account
before relying on this in production, same caveat as
app/services/dhl_service.py carries for DHL.

Amount handling note: `invoice.paid`'s amount_paid is treated as the GROSS
amount actually charged. The invoice splits it with billing_service.invoice_vat_rate():
no VAT for a small business (§ 19 UStG, the default), otherwise 19 %. The Stripe
price must be set up as the final amount the customer pays.
"""

import logging
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import get_current_user_ignoring_trial, get_db
from app.models.invoice import Invoice
from app.models.plan import Plan
from app.models.tenant import Tenant
from app.models.user import User
from app.routers.admin import _get_or_create_settings
from app.schemas.admin import InvoiceOut
from app.schemas.billing import CheckoutSessionOut, CheckoutSessionRequest, PortalSessionOut
from app.schemas.tenant import BillingProfile, BillingProfileOut
from app.services.billing_service import (
    BILLING_COUNTRIES, BILLING_REQUIRED, billing_profile_complete, deliver_invoice, invoice_vat_rate,
    issue_invoice, require_invoice_data, stored_pdf, subscription_description, welcome_once,
)
from app.services.sales_invoice_service import InvoiceError, pdf_text

router = APIRouter(prefix="/billing", tags=["billing"])
log = logging.getLogger("billing")


def _require_stripe_configured() -> None:
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail="Stripe ist nicht konfiguriert.")
    stripe.api_key = settings.stripe_secret_key


@router.post("/checkout-session", response_model=CheckoutSessionOut)
def create_checkout_session(
    payload: CheckoutSessionRequest,
    current_user: User = Depends(get_current_user_ignoring_trial),
    db: Session = Depends(get_db),
):
    _require_stripe_configured()

    plan = db.query(Plan).filter(Plan.id == payload.plan_id, Plan.is_active == True).first()  # noqa: E712
    if not plan:
        raise HTTPException(status_code=404, detail="Plan nicht gefunden")
    if not plan.stripe_price_id_monthly:
        raise HTTPException(status_code=400, detail="Für diesen Plan ist noch kein Stripe-Preis hinterlegt.")

    tenant = db.query(Tenant).filter(Tenant.id == current_user.tenant_id).first()
    # Without both sides' details no valid invoice could be issued after the payment.
    try:
        require_invoice_data(tenant, _get_or_create_settings(db))
    except ValueError:
        if not billing_profile_complete(tenant):
            raise HTTPException(status_code=400, detail="Bitte zuerst unter Einstellungen die Rechnungsadresse "
                                                        "ausfüllen (derzeit nur Anschriften in Deutschland).")
        raise HTTPException(status_code=503, detail="Die Buchung ist gerade noch nicht möglich.")

    if not tenant.stripe_customer_id:
        customer = stripe.Customer.create(
            email=current_user.email,
            name=tenant.company_name,
            metadata={"tenant_id": str(tenant.id)},
        )
        tenant.stripe_customer_id = customer.id
        db.commit()

    session = stripe.checkout.Session.create(
        mode="subscription",
        customer=tenant.stripe_customer_id,
        line_items=[{"price": plan.stripe_price_id_monthly, "quantity": 1}],
        success_url=f"{settings.frontend_url}/app/settings.html?billing=success",
        cancel_url=f"{settings.frontend_url}/app/settings.html?billing=cancelled",
        client_reference_id=str(tenant.id),
        metadata={"tenant_id": str(tenant.id), "plan_id": str(plan.id)},
    )
    return CheckoutSessionOut(url=session.url)


@router.post("/portal-session", response_model=PortalSessionOut)
def create_portal_session(
    current_user: User = Depends(get_current_user_ignoring_trial),
    db: Session = Depends(get_db),
):
    _require_stripe_configured()

    tenant = db.query(Tenant).filter(Tenant.id == current_user.tenant_id).first()
    if not tenant.stripe_customer_id:
        raise HTTPException(
            status_code=400,
            detail="Noch kein Stripe-Kunde für dieses Konto - bitte zuerst ein Abo abschließen.",
        )

    session = stripe.billing_portal.Session.create(
        customer=tenant.stripe_customer_id,
        return_url=f"{settings.frontend_url}/app/settings.html",
    )
    return PortalSessionOut(url=session.url)


def _latest(timestamps) -> datetime | None:
    """The latest of Stripe's Unix timestamps, or None if there is none."""
    values = [value for value in timestamps if value]
    return datetime.fromtimestamp(max(values), tz=timezone.utc) if values else None


def _subscription_period_end(subscription: dict) -> datetime | None:
    # older API versions report the period on the subscription, newer ones on each item
    items = (subscription.get("items") or {}).get("data") or []
    return _latest([subscription.get("current_period_end")] + [item.get("current_period_end") for item in items])


def _invoice_period_end(stripe_invoice: dict) -> datetime | None:
    lines = (stripe_invoice.get("lines") or {}).get("data") or []
    return _latest([(line.get("period") or {}).get("end") for line in lines])


@router.get("/profile", response_model=BillingProfileOut)
def get_billing_profile(
    current_user: User = Depends(get_current_user_ignoring_trial),
    db: Session = Depends(get_db),
):
    tenant = db.query(Tenant).filter(Tenant.id == current_user.tenant_id).first()
    return _profile_out(tenant)


@router.put("/profile", response_model=BillingProfileOut)
def save_billing_profile(
    payload: BillingProfile,
    current_user: User = Depends(get_current_user_ignoring_trial),
    db: Session = Depends(get_db),
):
    """Used on invoices issued from now on; issued invoices stay unchanged."""
    values = {key: (value or "").strip() for key, value in payload.model_dump().items()}
    values["billing_country"] = values["billing_country"].upper()
    if not all(values[key] for key in BILLING_REQUIRED):
        raise HTTPException(status_code=400, detail="Bitte Name, Straße, Postleitzahl, Ort und Land ausfüllen.")
    if values["billing_country"] not in BILLING_COUNTRIES:
        raise HTTPException(status_code=400, detail="Derzeit sind nur Rechnungsadressen in Deutschland möglich.")
    if not re.fullmatch(r"\d{5}", values["billing_zip"]):
        raise HTTPException(status_code=400, detail="Die Postleitzahl muss aus 5 Ziffern bestehen.")
    try:  # refuse now what the invoice PDF could not print after the payment
        for value in values.values():
            pdf_text(value)
    except InvoiceError as err:
        raise HTTPException(status_code=400, detail=f"{err} Bitte in lateinischen Buchstaben schreiben.")

    tenant = db.query(Tenant).filter(Tenant.id == current_user.tenant_id).first()
    for key, value in values.items():
        setattr(tenant, key, value or None)
    db.commit()
    return _profile_out(tenant)


def _profile_out(tenant: Tenant) -> BillingProfileOut:
    fields = {key: getattr(tenant, key) for key in BillingProfile.model_fields}
    return BillingProfileOut(**fields, complete=billing_profile_complete(tenant))


@router.get("/invoices", response_model=list[InvoiceOut])
def list_my_invoices(
    current_user: User = Depends(get_current_user_ignoring_trial),
    db: Session = Depends(get_db),
):
    return (db.query(Invoice).filter(Invoice.tenant_id == current_user.tenant_id)
            .order_by(Invoice.invoice_number.desc()).all())


@router.get("/invoices/{invoice_id}/pdf")
def download_my_invoice(
    invoice_id: uuid.UUID,
    current_user: User = Depends(get_current_user_ignoring_trial),
    db: Session = Depends(get_db),
):
    invoice = db.query(Invoice).filter(
        Invoice.id == invoice_id, Invoice.tenant_id == current_user.tenant_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Rechnung nicht gefunden")
    return Response(
        content=stored_pdf(db, invoice, _get_or_create_settings(db)),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="Rechnung-{invoice.invoice_number}.pdf"',
                 "Cache-Control": "no-store"},
    )


def _handle_checkout_completed(db: Session, session: dict) -> None:
    tenant_id = session.get("client_reference_id") or (session.get("metadata") or {}).get("tenant_id")
    if not tenant_id:
        return
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        return

    tenant.stripe_customer_id = session.get("customer") or tenant.stripe_customer_id
    tenant.stripe_subscription_id = session.get("subscription")
    tenant.subscription_status = "active"
    plan_id = (session.get("metadata") or {}).get("plan_id")
    plan = db.query(Plan).filter(Plan.id == plan_id).first() if plan_id else None
    if plan:
        tenant.plan_id = plan.id
    tenant.trial_ends_at = None  # a real subscription supersedes the trial clock
    db.commit()


def _handle_subscription_updated(db: Session, subscription: dict) -> None:
    tenant = db.query(Tenant).filter(Tenant.stripe_subscription_id == subscription.get("id")).first()
    if not tenant:
        return
    tenant.subscription_status = subscription.get("status")
    tenant.current_period_end = _subscription_period_end(subscription) or tenant.current_period_end
    db.commit()


def _stripe_line(stripe_invoice: dict) -> dict:
    lines = (stripe_invoice.get("lines") or {}).get("data") or []
    return lines[0] if lines else {}


def _service_period(stripe_invoice: dict) -> tuple[date, date] | None:
    """The period paid for, from the subscription line. (The invoice's own
    period_start/period_end describe the previous period, so they aren't used.)"""
    period = _stripe_line(stripe_invoice).get("period") or {}
    if not period.get("start") or not period.get("end"):
        return None
    berlin = ZoneInfo("Europe/Berlin")
    start = datetime.fromtimestamp(period["start"], berlin).date()
    # Stripe's end is the moment the next period begins.
    end = datetime.fromtimestamp(period["end"], berlin).date() - timedelta(days=1)
    return (start, end) if end >= start else None


def _plan_for(db: Session, tenant: Tenant, stripe_invoice: dict) -> Plan | None:
    """The plan actually charged; the account's plan if the price is unknown."""
    line = _stripe_line(stripe_invoice)
    price_id = (line.get("price") or {}).get("id") \
        or ((line.get("pricing") or {}).get("price_details") or {}).get("price")
    if price_id:
        plan = db.query(Plan).filter(Plan.stripe_price_id_monthly == price_id).first()
        if plan:
            return plan
    return db.query(Plan).filter(Plan.id == tenant.plan_id).first() if tenant.plan_id else None


def _handle_invoice_paid(db: Session, stripe_invoice: dict) -> None:
    """
    Fires on every successful subscription charge (first and every
    renewal) - each becomes an invoice via the same path
    app/routers/admin.py::confirm_payment uses for a manual payment. See
    this module's docstring for the amount-handling assumption.

    If the invoice or its email fails, the webhook answers 500 so that
    Stripe delivers the event again; a payment already invoiced is never
    invoiced twice, only an unsent email is sent.
    """
    customer_id = stripe_invoice.get("customer")
    tenant = db.query(Tenant).filter(Tenant.stripe_customer_id == customer_id).first() if customer_id else None
    if not tenant:
        return

    # a paid invoice only ever moves the end forward (events can arrive out of order)
    period_end = _invoice_period_end(stripe_invoice)
    if period_end and (tenant.current_period_end is None or period_end > tenant.current_period_end):
        tenant.current_period_end = period_end
        db.commit()

    amount_paid = stripe_invoice.get("amount_paid") or 0
    if amount_paid <= 0:  # e.g. the start of a trial: nothing was charged
        return

    owner = (
        db.query(User)
        .filter(User.tenant_id == tenant.id)
        .order_by(User.created_at.asc())
        .first()
    )
    if not owner:
        return

    settings_row = _get_or_create_settings(db)
    plan = _plan_for(db, tenant, stripe_invoice)
    plan_name = plan.name if plan else None
    stripe_id = stripe_invoice.get("id")

    try:
        existing = db.query(Invoice).filter(Invoice.stripe_invoice_id == stripe_id).first() if stripe_id else None
        if existing:
            welcome_once(db, tenant, owner, plan_name, settings_row.company_legal_name)
            if existing.email_sent_at is None:
                deliver_invoice(db, existing, settings_row)
            return

        gross_amount = Decimal(amount_paid) / 100
        net_amount = (gross_amount / (1 + invoice_vat_rate() / 100)).quantize(Decimal("0.01"))
        currency = (stripe_invoice.get("currency") or "eur").upper()
        issue_invoice(db, tenant, owner, settings_row, net_amount, subscription_description(plan_name), currency,
                      service_period=_service_period(stripe_invoice), payment_method="stripe",
                      stripe_invoice_id=stripe_id, plan_name=plan_name)
    except (ValueError, RuntimeError) as err:
        log.error("invoice.paid handling failed for tenant %s: %s", tenant.id, err)
        raise HTTPException(status_code=500, detail="Rechnung noch nicht zugestellt - bitte erneut senden.")


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    if not settings.stripe_secret_key or not settings.stripe_webhook_secret:
        raise HTTPException(status_code=503, detail="Stripe ist nicht konfiguriert.")
    stripe.api_key = settings.stripe_secret_key

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, settings.stripe_webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event_type = event["type"]
    data = event["data"]["object"]

    if event_type == "checkout.session.completed":
        _handle_checkout_completed(db, data)
    elif event_type == "invoice.paid":
        _handle_invoice_paid(db, data)
    elif event_type in ("customer.subscription.updated", "customer.subscription.deleted"):
        _handle_subscription_updated(db, data)

    return {"received": True}
