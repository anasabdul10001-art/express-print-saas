"""Keeps NettoDesk up to date with eBay while the seller is using it.

One run reads, in this order: new orders, new listings (and missing pictures), then
the available quantities. eBay is only read. Runs for one tenant never overlap
(two of them could import the same order twice).

Quantities are taken over from eBay unless the tenant also sells the goods through
Shopify or Amazon: then eBay alone does not know the right stock.
"""

import logging
import threading
import time
from collections import defaultdict

from sqlalchemy.orm import Session

from app.models.amazon_account import AmazonAccount
from app.models.ebay_account import EbayAccount
from app.models.shopify_account import ShopifyAccount
from app.services import ebay_listing_service, ebay_order_service, ebay_stock_service

log = logging.getLogger("live_sync")

# Several open pages or tabs share one run; eBay also limits the number of calls per day.
MIN_SECONDS_BETWEEN_RUNS = 60

_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_last_run: dict[str, float] = {}


def lock_for(tenant_id) -> threading.Lock:
    """Held by everything that imports from eBay, in this process."""
    return _locks[str(tenant_id)]


def postpone(tenant_id) -> None:
    """The next run waits a full interval from now."""
    _last_run[str(tenant_id)] = time.monotonic()


def sells_elsewhere(db: Session, tenant_id) -> bool:
    return any(
        db.query(model.id).filter(model.tenant_id == tenant_id, model.status == "CONNECTED").first()
        for model in (ShopifyAccount, AmazonAccount))


def run(db: Session, tenant_id) -> dict:
    result = {"ran": False, "connected": True, "new_orders": 0, "new_products": 0,
              "quantities_updated": 0, "errors": []}
    lock = lock_for(tenant_id)
    if not lock.acquire(blocking=False):
        return result
    try:
        last = _last_run.get(str(tenant_id))
        if last is not None and time.monotonic() - last < MIN_SECONDS_BETWEEN_RUNS:
            return result
        accounts = db.query(EbayAccount).filter(
            EbayAccount.tenant_id == tenant_id, EbayAccount.status == "CONNECTED").all()
        if not accounts:
            result["connected"] = False
            return result
        _last_run[str(tenant_id)] = time.monotonic()
        result["ran"] = True
        _run_accounts(db, tenant_id, accounts, result)
        return result
    finally:
        lock.release()


def _run_accounts(db: Session, tenant_id, accounts: list, result: dict) -> None:
    listings: list[dict] = []
    complete = True
    for account in accounts:
        name = account.ebay_user_id or "eBay"
        try:
            # Orders first: eBay has already subtracted them from the listing, so
            # importing them after the quantities would subtract them a second time.
            result["new_orders"] += ebay_order_service.sync_orders(account, tenant_id, db)["created"]
            found = ebay_listing_service.fetch_active_listings(account, db)
            imported = ebay_listing_service.import_listings(account, tenant_id, db, found)
            result["new_products"] += imported["products_created"]
            listings += found
        except Exception as exc:  # one broken account must not stop the others
            db.rollback()
            complete = False
            log.warning("live sync failed for %s: %s", account.id, exc)
            result["errors"].append(f"{name}: {str(exc)[:200]}")

    # With one account missing, goods listed on two accounts would look unambiguous.
    if not complete or sells_elsewhere(db, tenant_id):
        return
    differences = ebay_stock_service.compare(db, tenant_id, listings)["items"]
    if differences:
        confirmed = {row["product_id"]: row["ebay_quantity"] for row in differences}
        result["quantities_updated"], _ = ebay_stock_service.apply(db, tenant_id, differences, confirmed)
