"""
Things the seller should act on, worked out on demand from data we already have.
Nothing is stored or sent anywhere: an alert disappears once its cause is gone.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models.amazon_account import AmazonAccount
from app.models.ebay_account import EbayAccount
from app.models.order import Order, OrderItem
from app.models.product import Product
from app.models.shopify_account import ShopifyAccount
from app.services import order_money

SALES_WINDOW_DAYS = 30     # basis for "how fast does this sell"
LOW_STOCK_DAYS = 14        # warn when the stock lasts no longer than this
SHIP_AFTER_HOURS = 24      # an order this old should have been shipped
SHIP_WINDOW_DAYS = 14      # older ones are more likely unsynced history than forgotten parcels
TOKEN_WARNING_DAYS = 30
MAX_ITEMS = 5
SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _alert(kind: str, severity: str, title: str, items: list[str], link: str) -> dict:
    return {"type": kind, "severity": severity, "title": title, "count": len(items),
            "items": items[:MAX_ITEMS], "link": link}


def _short(text: str) -> str:
    return text if len(text) <= 70 else text[:69] + "…"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def build_alerts(db: Session, tenant_id, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    sold_at = func.coalesce(Order.ordered_at, Order.created_at)
    orders = (
        db.query(Order)
        .options(joinedload(Order.items).joinedload(OrderItem.product))
        .filter(Order.tenant_id == tenant_id, Order.status != "canceled",
                sold_at >= now - timedelta(days=SALES_WINDOW_DAYS))
        .all()
    )
    alerts = []

    # Orders waiting to be shipped.
    waiting = []
    for order in orders:
        age = now - _aware(order.ordered_at or order.created_at)
        if (order.status in ("new", "ready_to_print", "printed")
                and timedelta(hours=SHIP_AFTER_HOURS) <= age <= timedelta(days=SHIP_WINDOW_DAYS)
                and order.ebay_payment_status != "FULLY_REFUNDED"):
            waiting.append((age, order.external_order_ref or "ohne Referenz"))
    if waiting:
        waiting.sort(reverse=True)
        alerts.append(_alert("unshipped", "critical", "Bestellungen warten auf den Versand",
                             [f"{ref} (seit {age.days} Tg.)" for age, ref in waiting], "orders.html"))

    # Connections that stopped working or are about to.
    broken, expiring = [], []
    for label, model, name in (("eBay", EbayAccount, "ebay_user_id"), ("Shopify", ShopifyAccount, "shop_domain"),
                               ("Amazon", AmazonAccount, "seller_id")):
        for account in db.query(model).filter(model.tenant_id == tenant_id).all():
            who = f"{label}: {getattr(account, name) or 'Konto'}"
            if account.status == "ERROR":
                broken.append(who)
            expires = getattr(account, "refresh_token_expires_at", None)
            if account.status == "CONNECTED" and expires:
                left = _aware(expires) - now
                if left <= timedelta(days=TOKEN_WARNING_DAYS):
                    expiring.append(f"{who} (noch {max(left.days, 0)} Tg.)")
    if broken:
        alerts.append(_alert("connection_error", "critical", "Verbindung gestört – bitte neu verbinden",
                             broken, "dashboard.html"))
    if expiring:
        alerts.append(_alert("connection_expiring", "warning", "Verbindung läuft bald ab", expiring, "dashboard.html"))

    # Stock, judged by what actually sold.
    sold: dict = {}
    for order in orders:
        for item in order.items:
            if item.product_id:
                sold[item.product_id] = sold.get(item.product_id, 0) + item.quantity
    products = db.query(Product).filter(Product.tenant_id == tenant_id, Product.active.is_(True),
                                        Product.unlisted.is_(False)).all()
    empty, low = [], []
    for product in products:
        units = sold.get(product.id, 0)
        if not units:
            continue
        if product.stock_quantity <= 0:
            empty.append((-units, f"{_short(product.title)} ({units} verkauft in {SALES_WINDOW_DAYS} Tg.)"))
        else:
            days_left = product.stock_quantity * SALES_WINDOW_DAYS / units
            if days_left <= LOW_STOCK_DAYS:
                low.append((days_left, f"{_short(product.title)} ({product.stock_quantity} Stück, "
                                       f"reicht ca. {max(int(days_left), 1)} Tg.)"))
    if empty:
        alerts.append(_alert("out_of_stock", "critical", "Verkaufsstarke Artikel sind ausverkauft",
                             [text for _, text in sorted(empty)], "products.html"))
    if low:
        alerts.append(_alert("low_stock", "warning", "Bestand wird bald knapp",
                             [text for _, text in sorted(low)], "products.html"))

    # Orders that lost money (only where every cost is known).
    losses = []
    for order in orders:
        if order.items and all(item.purchase_price is not None for item in order.items):
            result = order_money.profit(order)
            if result < 0:
                losses.append((result, order.external_order_ref or "ohne Referenz"))
    if losses:
        alerts.append(_alert("loss", "warning", f"Bestellungen mit Verlust (letzte {SALES_WINDOW_DAYS} Tage)",
                             [f"{ref}: {result:.2f} €".replace(".", ",") for result, ref in sorted(losses)],
                             "orders.html"))

    # Without a purchase price there is no real profit figure.
    no_cost = [_short(p.title) for p in products
               if p.purchase_price is None and (p.stock_quantity > 0 or p.id in sold)]
    if no_cost:
        alerts.append(_alert("missing_cost", "info", "Einkaufspreis fehlt – Gewinn ist unvollständig",
                             no_cost, "products.html"))

    return sorted(alerts, key=lambda a: SEVERITY_ORDER[a["severity"]])
