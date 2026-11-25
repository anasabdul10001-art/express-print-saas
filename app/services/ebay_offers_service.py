"""
Offers to interested buyers (eBay Sell Negotiation API).

find_eligible_items only reads. send_offer really sends a discount to buyers, so it
is only ever called after the seller confirmed that one offer; it is never retried
or run in the background.

Request limits taken from eBay's API contract: one listing per request, at least
5 % discount, offer valid 2 days (fixed by eBay), no counter offers, message up to
2000 characters. Listings created through the Inventory API are not supported.
"""

from decimal import ROUND_HALF_UP, Decimal

import httpx
from sqlalchemy.orm import Session

from app.models.ebay_account import EbayAccount
from app.models.product import Product
from app.services import ebay_oauth_service, pricing
from app.services.ebay_extras_service import API, MARKETPLACE

BASE = "/sell/negotiation/v1"
PAGE_SIZE = 200
MAX_PAGES = 5
# eBay's minimum is 5 %; the upper limit is ours, against typing mistakes.
MIN_DISCOUNT, MAX_DISCOUNT = 5, 50
DEFAULT_LABEL_COST = Decimal("1.10")


class SendUncertain(Exception):
    """No answer from eBay: the offer may or may not have been sent."""


def _headers(account: EbayAccount, db: Session) -> dict:
    token = ebay_oauth_service.get_valid_access_token(account, db)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE}


def _error_text(response: httpx.Response) -> str:
    try:
        errors = response.json().get("errors") or []
        text = "; ".join(e.get("longMessage") or e.get("message") or "" for e in errors).strip("; ")
    except (ValueError, AttributeError):
        text = ""
    return f"eBay antwortet {response.status_code}: {text or response.text[:200]}"


def eligible_listing_ids(account: EbayAccount, db: Session) -> list[str]:
    headers = _headers(account, db)
    ids: list[str] = []
    for page in range(MAX_PAGES):
        response = httpx.get(f"{API[account.environment]}{BASE}/find_eligible_items", headers=headers,
                             params={"limit": PAGE_SIZE, "offset": page * PAGE_SIZE}, timeout=20.0)
        if response.status_code == 204:  # nothing is eligible
            break
        if response.status_code != 200:
            raise ValueError(_error_text(response))
        data = response.json()
        ids += [item["listingId"] for item in data.get("eligibleItems", []) if item.get("listingId")]
        if not data.get("next"):
            break
    return ids


def product_for_listing(db: Session, tenant_id, listing_id: str) -> Product | None:
    """Imported listings without a custom label use the eBay item number as SKU."""
    return db.query(Product).filter(Product.tenant_id == tenant_id, Product.sku == listing_id).first()


def price_check(product: Product | None, vat_mode: str) -> dict:
    """Current price and break-even price as far as we know them. An estimate: the stored
    price can differ from eBay's, and label cost and fee rate may be defaults."""
    if product is None:
        return {"title": None, "price": None, "break_even": None}
    break_even = None
    if product.unit_cost is not None:
        try:
            break_even = pricing.solve_price(
                goods_cost=product.unit_cost, label_cost=DEFAULT_LABEL_COST,
                fee_percent=product.ebay_fee_percent if product.ebay_fee_percent is not None else pricing.DEFAULT_FEE_PERCENT,
                fee_fixed=pricing.DEFAULT_FEE_FIXED, vat_mode=vat_mode, vat_rate=pricing.DEFAULT_VAT_RATE)
        except pricing.PricingError:
            break_even = None
    return {"title": product.title, "price": product.selling_price, "break_even": break_even}


def discounted(price: Decimal, percent: int) -> Decimal:
    return (price * (100 - percent) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def send_offer(account: EbayAccount, db: Session, listing_id: str, percent: int, message: str) -> int:
    """Sends one offer for one listing; returns how many buyers received it."""
    body = {
        "allowCounterOffer": False,
        "offeredItems": [{"listingId": listing_id, "discountPercentage": str(percent), "quantity": 1}],
    }
    if message:
        body["message"] = message
    headers = _headers(account, db)
    try:
        response = httpx.post(f"{API[account.environment]}{BASE}/send_offer_to_interested_buyers",
                              headers=headers, json=body, timeout=30.0)
    except httpx.HTTPError as exc:
        raise SendUncertain(type(exc).__name__) from exc
    if response.status_code != 200:
        raise ValueError(_error_text(response))
    return len(response.json().get("offers", []))
