"""Days left of the free trial and of the paid billing period."""
import uuid
from datetime import datetime, timedelta, timezone

import stripe

from app.config import settings
from app.database import SessionLocal
from app.models.tenant import Tenant


def _set(headers, client, **fields):
    tenant_id = client.get("/tenants/me", headers=headers).json()["id"]
    with SessionLocal() as db:
        tenant = db.query(Tenant).filter(Tenant.id == uuid.UUID(tenant_id)).first()
        for name, value in fields.items():
            setattr(tenant, name, value)
        db.commit()


def _me(client, headers):
    return client.get("/tenants/me", headers=headers).json()


def _in(**delta):
    return datetime.now(timezone.utc) + timedelta(**delta)


def _webhook(client, monkeypatch, event_type, obj):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_fake")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_fake")
    monkeypatch.setattr(stripe.Webhook, "construct_event",
                        lambda payload, sig, secret: {"type": event_type, "data": {"object": obj}})
    r = client.post("/billing/webhooks/stripe", content=b"{}", headers={"stripe-signature": "valid"})
    assert r.status_code == 200, r.text


def test_without_trial_or_subscription_there_is_nothing_to_count(client, register):
    headers, _ = register()
    _set(headers, client, trial_ends_at=None)
    me = _me(client, headers)
    assert me["trial_days_left"] is None and me["period_days_left"] is None


def test_trial_days_count_every_started_day(client, register):
    headers, _ = register()
    _set(headers, client, trial_ends_at=_in(days=14, minutes=-1))
    assert _me(client, headers)["trial_days_left"] == 14
    _set(headers, client, trial_ends_at=_in(hours=2))
    assert _me(client, headers)["trial_days_left"] == 1


def test_an_active_subscription_counts_the_paid_period_instead(client, register):
    headers, _ = register()
    _set(headers, client, trial_ends_at=_in(days=3), subscription_status="active",
         current_period_end=_in(days=20, minutes=-1))
    me = _me(client, headers)
    assert me["trial_days_left"] is None and me["period_days_left"] == 20
    _set(headers, client, subscription_status="canceled", trial_ends_at=None)
    assert _me(client, headers)["period_days_left"] is None


def test_stripe_events_store_the_period_end(client, register, monkeypatch):
    headers, _ = register()
    _set(headers, client, stripe_subscription_id="sub_days", stripe_customer_id="cus_days", trial_ends_at=None)
    end = int(_in(days=30, minutes=-1).timestamp())

    # newer API versions report the period on the items
    _webhook(client, monkeypatch, "customer.subscription.updated",
             {"id": "sub_days", "status": "active", "items": {"data": [{"current_period_end": end}]}})
    assert _me(client, headers)["period_days_left"] == 30

    # a paid invoice for the next month moves the end forward, an older one does not move it back
    for timestamp, expected in ((end + 30 * 86400, 60), (end - 10 * 86400, 60)):
        _webhook(client, monkeypatch, "invoice.paid",
                 {"customer": "cus_days", "amount_paid": 0, "currency": "eur",
                  "lines": {"data": [{"period": {"end": timestamp}}]}})
        assert _me(client, headers)["period_days_left"] == expected


def test_an_event_without_a_period_keeps_the_stored_end(client, register, monkeypatch):
    headers, _ = register()
    _set(headers, client, stripe_subscription_id="sub_keep", subscription_status="active",
         current_period_end=_in(days=5, minutes=-1))
    _webhook(client, monkeypatch, "customer.subscription.updated", {"id": "sub_keep", "status": "active"})
    assert _me(client, headers)["period_days_left"] == 5
