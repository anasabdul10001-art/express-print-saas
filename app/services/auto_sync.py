"""Optional background loop: pulls new eBay orders for every connected
account every N minutes (settings.auto_sync_minutes, 0 = off)."""

import logging
import threading
import time

from app.config import settings
from app.database import SessionLocal
from app.models.ebay_account import EbayAccount
from app.services import ebay_order_service, live_sync

log = logging.getLogger("auto_sync")


def run_once() -> int:
    synced = 0
    with SessionLocal() as db:
        for account in db.query(EbayAccount).filter(EbayAccount.status == "CONNECTED").all():
            try:
                with live_sync.lock_for(account.tenant_id):
                    ebay_order_service.sync_orders(account, account.tenant_id, db)
                synced += 1
            except Exception as exc:  # one broken account must not stop the loop
                db.rollback()
                log.warning("auto sync failed for %s: %s", account.id, exc)
    return synced


def start() -> None:
    minutes = settings.auto_sync_minutes
    if minutes <= 0:
        return

    def loop():
        while True:
            time.sleep(minutes * 60)
            run_once()

    threading.Thread(target=loop, daemon=True, name="auto-sync").start()
