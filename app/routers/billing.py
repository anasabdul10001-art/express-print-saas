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
amount actually charged, with net/VAT re-derived at this app's fixed 19%
rate (INVOICE_VAT_RATE) so every invoice this app issues stays internally
consistent, regardless of how the Stripe Price itself is configured
(tax-inclusive vs. exclusive). [PLATZHALTER: verify this matches the real
Stripe Price setup - and how EU B2B reverse-charge customers, if any,
should be handled - before going live.]
"""

from decimal import Decimal

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import get_current_user_ignoring_trial, get_db
from app.models.plan import Plan
from app.models.tenant import Tenant
from app.models.user import User
from app.routers.admin import _get_or_create_settings
from app.schemas.billing import CheckoutSessionOut, CheckoutSessionRequest, PortalSessionOut
from app.services.billing_service import INVOICE_VAT_RATE, issue_invoice

router = APIRouter(prefix="/billing", tags=["billing"])


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
    tenant.trial_ends_at = None  # a real subscription supersedes the trial clock
    db.commit()


def _handle_subscription_updated(db: Session, subscription: dict) -> None:
    tenant = db.query(Tenant).filter(Tenant.stripe_subscription_id == subscription.get("id")).first()
    if not tenant:
        return
    tenant.subscription_status = subscription.get("status")
    db.commit()


def _handle_invoice_paid(db: Session, stripe_invoice: dict) -> None:
    """
    Fires on every successful subscription charge (first and every
    renewal) - each becomes a real §14-UStG invoice via the same path
    app/routers/admin.py::confirm_payment uses for a manual payment. See
    this module's docstring for the amount-handling assumption.
    """
    customer_id = stripe_invoice.get("customer")
    tenant = db.query(Tenant).filter(Tenant.stripe_customer_id == customer_id).first()
    if not tenant:
        return

    owner = (
        db.query(User)
        .filter(User.tenant_id == tenant.id)
        .order_by(User.created_at.asc())
        .first()
    )
    if not owner:
        return

    gross_amount = Decimal(stripe_invoice.get("amount_paid", 0)) / 100
    net_amount = (gross_amount / (1 + INVOICE_VAT_RATE / 100)).quantize(Decimal("0.01"))
    currency = (stripe_invoice.get("currency") or "eur").upper()

    settings_row = _get_or_create_settings(db)
    plan = db.query(Plan).filter(Plan.id == tenant.plan_id).first() if tenant.plan_id else None
    description = plan.name if plan else "Abonnement"

    try:
        issue_invoice(db, tenant, owner, settings_row, net_amount, description, currency)
    except (ValueError, RuntimeError) as err:
        # Never fail the webhook response over a downstream config/email
        # gap - Stripe already charged the customer. Logged for follow-up
        # rather than retried, since Stripe would just redeliver the same
        # event and hit the same gap again.
        print(f"[stripe webhook] invoice.paid handling failed for tenant {tenant.id}: {err}")


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
