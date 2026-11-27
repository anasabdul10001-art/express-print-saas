"""A quantity changed in NettoDesk is sent to the eBay listing.

Without this the next sync would put eBay's old number back. Called after the
change is saved, only for what the seller did in NettoDesk (never for sales that
came from eBay: eBay has already counted those).

Two kinds of change:
- "set": the seller typed the available quantity; exactly that number is sent.
- "add": goods came in or went out (restock, purchase, own order, return). The
  difference is applied to what eBay reports right now, so a sale NettoDesk has
  not read yet is not undone.

It never raises: the change in NettoDesk is already saved, so the caller only
reports what happened at eBay.
"""

import logging

from sqlalchemy.orm import Session

from app.models.ebay_account import EbayAccount
from app.models.product import Product
from app.models.stock_movement import StockMovement
from app.services import ebay_listing_service, ebay_stock_service, live_sync

log = logging.getLogger("ebay_quantity_push")


def set_to(product_id, quantity: int) -> tuple:
    return (str(product_id), "set", int(quantity))


def add(product_id, difference: int) -> tuple:
    return (str(product_id), "add", int(difference))


def send(db: Session, tenant_id, changes: list[tuple]) -> list[dict]:
    """One result per product that eBay could be concerned by:
    {product_id, title, status: ok|unchanged|no_listing|ambiguous|failed, quantity, error}.
    Empty if there is no connected eBay account."""
    wanted: dict[str, tuple[str, int]] = {}
    for product_id, kind, number in changes:
        if kind == "add" and product_id in wanted:
            wanted[product_id] = (wanted[product_id][0], wanted[product_id][1] + number)
        else:
            wanted[product_id] = (kind, number)
    wanted = {pid: v for pid, v in wanted.items() if v != ("add", 0)}
    if not wanted:
        return []
    try:
        accounts = db.query(EbayAccount).filter(
            EbayAccount.tenant_id == tenant_id, EbayAccount.status == "CONNECTED").all()
        if not accounts:
            return []
        products = db.query(Product).filter(
            Product.tenant_id == tenant_id, Product.id.in_(list(wanted)),
            Product.active.is_(True), Product.unlisted.is_(False)).all()
        targets = [(str(p.id), p.title) for p in products]
    except Exception as exc:
        db.rollback()
        log.warning("quantity push could not start: %s", exc)
        return [_result(pid, "", "failed", error=str(exc)[:200]) for pid in wanted]
    if not targets:
        return []
    # Held so that no sync reads the old number now and writes it back afterwards.
    with live_sync.lock_for(tenant_id):
        try:
            return _send_locked(db, tenant_id, accounts, targets, wanted)
        except Exception as exc:
            db.rollback()
            log.warning("quantity push failed: %s", exc)
            return [_result(pid, title, "failed", error=str(exc)[:200]) for pid, title in targets]


def _result(product_id, title, status, quantity=None, error=None) -> dict:
    return {"product_id": product_id, "title": title, "status": status, "quantity": quantity, "error": error}


def _send_locked(db: Session, tenant_id, accounts: list, targets: list, wanted: dict) -> list[dict]:
    listings: list[dict] = []
    owner: dict[str, EbayAccount] = {}
    for account in accounts:  # all of them, else a listing on two accounts would look like one
        for listing in ebay_listing_service.fetch_active_listings(account, db):
            listings.append(listing)
            owner[listing["item_id"]] = account
    found = {str(product.id): matches
             for product, matches in ebay_stock_service.match(db, tenant_id, listings)[1].values()}
    shipsync_follows = not live_sync.sells_elsewhere(db, tenant_id)

    results, todo = [], {}
    for product_id, title in targets:
        matches = found.get(product_id, [])
        if not matches:
            results.append(_result(product_id, title, "no_listing"))
            continue
        if len(matches) > 1:
            results.append(_result(product_id, title, "ambiguous"))
            continue
        listing = matches[0]
        kind, number = wanted[product_id]
        quantity = max(0, number if kind == "set" else listing["available"] + number)
        if quantity == listing["available"]:
            results.append(_result(product_id, title, "unchanged", quantity))
            _store(db, tenant_id, product_id, quantity, listing["item_id"], kind == "set" or shipsync_follows)
            continue
        todo[product_id] = (title, listing["item_id"], quantity, kind)

    by_account: dict = {}
    for product_id, (_, item_id, quantity, _) in todo.items():
        by_account.setdefault(owner[item_id].id, (owner[item_id], []))[1].append((item_id, None, quantity))
    answers: dict[str, str | None] = {}
    for account, items in by_account.values():
        try:
            answers.update(ebay_listing_service.revise_listings(account, db, items))
        except Exception as exc:
            db.rollback()
            answers.update({item_id: str(exc)[:200] for item_id, _, _ in items})

    for product_id, (title, item_id, quantity, kind) in todo.items():
        error = answers.get(item_id, "Keine Antwort von eBay.")
        if error is None:
            results.append(_result(product_id, title, "ok", quantity))
            _store(db, tenant_id, product_id, quantity, item_id, kind == "set" or shipsync_follows)
        else:
            results.append(_result(product_id, title, "failed", quantity, error))
    db.commit()
    live_sync.postpone(tenant_id)  # eBay may need a moment until its list shows the new number
    return results


def _store(db: Session, tenant_id, product_id: str, quantity: int, item_id: str, follow: bool) -> None:
    """NettoDesk shows what eBay now has."""
    if not follow:
        return
    product = db.query(Product).filter(Product.tenant_id == tenant_id, Product.id == product_id)\
        .populate_existing().one()
    if product.stock_quantity != quantity:
        db.add(StockMovement(tenant_id=tenant_id, product_id=product.id,
                             quantity_change=quantity - product.stock_quantity,
                             reason=ebay_stock_service.REASON, reference=item_id))
        product.stock_quantity = quantity
    db.commit()
