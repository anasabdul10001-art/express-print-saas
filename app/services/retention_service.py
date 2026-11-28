"""
Deletes personal data once it is no longer needed (Art. 5(1)(e) GDPR).

- Buyer data on finished orders: user name, name, address, phone, tracking number
  and the stored labels, after the months the account chose (6 to 36).
  Amounts, items and dates stay for the bookkeeping; issued invoices are
  separate, unchanged documents with their own statutory retention.
- Stored print files of finished print jobs: after 90 days.
- Expired one-time tokens: right away.

Runs shortly after start and then once a day, so it hardly wakes the database.
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models.affiliate_password_token import AffiliatePasswordToken
from app.models.amazon_account import AmazonOAuthState
from app.models.ebay_account import EbayOAuthState
from app.models.internetmarke import InternetmarkePurchase, remove_buyer_data
from app.models.order import Order
from app.models.password_reset_token import PasswordResetToken
from app.models.print_job import PrintJob
from app.models.shopify_account import ShopifyOAuthState
from app.models.tenant import Tenant

log = logging.getLogger("retention")

CHOICES = (6, 12, 24, 36)
DEFAULT_MONTHS = 36
PRINT_FILE_DAYS = 90
BUYER_FIELDS = ("buyer_username", "recipient_name", "recipient_street1", "recipient_street2",
                "recipient_city", "recipient_state", "recipient_zip", "recipient_country_code",
                "recipient_phone", "tracking_number")


def months_before(moment: datetime, months: int) -> datetime:
    index = moment.year * 12 + (moment.month - 1) - months
    year, month = divmod(index, 12)
    last = [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month]
    return moment.replace(year=year, month=month + 1, day=min(moment.day, last))


def _remove_buyer_data(db: Session, now: datetime) -> int:
    removed = 0
    for tenant_id, months in db.query(Tenant.id, Tenant.buyer_data_retention_months).all():
        # an unexpected stored value must never delete earlier than the default
        cutoff = months_before(now, months if months in CHOICES else DEFAULT_MONTHS)
        ids = [i for (i,) in db.query(Order.id).filter(
            Order.tenant_id == tenant_id,
            Order.status.in_(("shipped", "canceled")),
            func.coalesce(Order.ordered_at, Order.created_at) < cutoff,
            or_(*[getattr(Order, name).isnot(None) for name in BUYER_FIELDS]),
        )]
        if not ids:
            continue
        db.query(Order).filter(Order.id.in_(ids)).update(
            {getattr(Order, name): None for name in BUYER_FIELDS}, synchronize_session=False)
        db.query(PrintJob).filter(PrintJob.order_id.in_(ids)).update(
            {PrintJob.label_pdf_data: None, PrintJob.error_message: None}, synchronize_session=False)
        remove_buyer_data(db, InternetmarkePurchase.order_id.in_(ids))
        removed += len(ids)
    return removed


def run(db: Session, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    orders = _remove_buyer_data(db, now)
    files = db.query(PrintJob).filter(
        PrintJob.status.in_(("printed", "failed")), PrintJob.label_pdf_data.isnot(None),
        PrintJob.created_at < now - timedelta(days=PRINT_FILE_DAYS),
    ).update({PrintJob.label_pdf_data: None, PrintJob.error_message: None}, synchronize_session=False)
    remove_buyer_data(db, InternetmarkePurchase.status.in_(("BOUGHT", "FAILED")),
                      InternetmarkePurchase.created_at < now - timedelta(days=PRINT_FILE_DAYS))
    tokens = 0
    for model in (PasswordResetToken, AffiliatePasswordToken, EbayOAuthState, AmazonOAuthState,
                  ShopifyOAuthState):
        tokens += db.query(model).filter(model.expires_at < now).delete(synchronize_session=False)
    db.commit()
    return {"orders": orders, "print_files": files, "tokens": tokens}


def start() -> None:
    if not settings.retention_enabled:
        return

    def loop():
        time.sleep(90)
        while True:
            try:
                with SessionLocal() as db:
                    log.info("retention: %s", run(db))
            except Exception as exc:  # noqa: BLE001 - try again tomorrow
                log.warning("retention failed: %s", exc)
            time.sleep(24 * 60 * 60)

    threading.Thread(target=loop, daemon=True, name="retention").start()
