"""
Market research: searches eBay's public active listings via the Browse API.

Deliberately NOT the Marketplace Insights API (the one Terapeak itself uses)
- that one is Limited Release and has been closed to new applicants since
around 2020 (see the "Marketplace Insights API access" thread on the eBay
developer forum). The Browse API is free and open to every registered
developer, but only covers *active* listings, not a true historical
sold-items index. `estimatedAvailabilities.estimatedSoldQuantity` on each
item is the closest free proxy for "how well is this selling" - it's an
eBay-computed estimate, not an exact count.

Uses the OAuth 2.0 "client credentials" grant - an application-level token
that identifies our app, not any particular tenant's connected eBay
account. This means market research works for every tenant without any of
them needing to connect eBay first, which is the right behavior since this
data isn't seller-specific.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings

# getItem (per-item detail lookup, needed for estimatedAvailabilities - see
# ENRICH_LIMIT note below) is a second network round-trip per item. Capped
# to keep search latency reasonable and avoid burning through the app's
# daily Browse API call quota on a single search.
ENRICH_LIMIT = 15

EBAY_TOKEN_URLS = {
    "SANDBOX": "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
    "PRODUCTION": "https://api.ebay.com/identity/v1/oauth2/token",
}
EBAY_BROWSE_BASE = {
    "SANDBOX": "https://api.sandbox.ebay.com",
    "PRODUCTION": "https://api.ebay.com",
}

# Read-only public browse/search scope - deliberately not the same scopes
# used for the per-tenant seller OAuth flow (sell.fulfillment/sell.inventory).
BROWSE_SCOPE = "https://api.ebay.com/oauth/api_scope"

# Cached in-process; a client-credentials token is app-wide, not per-tenant,
# so one cached token safely serves every tenant's requests.
_cached_token: str | None = None
_cached_token_expires_at: datetime | None = None


def _get_app_token() -> str:
    global _cached_token, _cached_token_expires_at

    now = datetime.now(timezone.utc)
    if _cached_token and _cached_token_expires_at and _cached_token_expires_at > now + timedelta(minutes=2):
        return _cached_token

    import base64
    raw = f"{settings.ebay_client_id}:{settings.ebay_client_secret}".encode()
    resp = httpx.post(
        EBAY_TOKEN_URLS[settings.ebay_environment],
        headers={
            "Authorization": f"Basic {base64.b64encode(raw).decode()}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "client_credentials", "scope": BROWSE_SCOPE},
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise ValueError(f"ebay_app_token_failed: {resp.text}")

    payload = resp.json()
    _cached_token = payload["access_token"]
    _cached_token_expires_at = now + timedelta(seconds=payload["expires_in"])
    return _cached_token


def _fetch_availability(item_id: str, token: str, marketplace: str, base_url: str) -> dict:
    """
    estimatedAvailabilities isn't included in the lightweight search
    response - it only comes back from a per-item getItem call. Returns an
    empty dict (never raises) on any failure, since one bad item must never
    break the rest of the search results.
    """
    try:
        resp = httpx.get(
            f"{base_url}/buy/browse/v1/item/{item_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": marketplace,
            },
            params={"fieldgroups": "COMPACT"},
            timeout=10.0,
        )
        if resp.status_code != 200:
            return {}
        availabilities = resp.json().get("estimatedAvailabilities") or []
        return availabilities[0] if availabilities else {}
    except httpx.HTTPError:
        return {}


def search_active_listings(query: str, limit: int = 30, marketplace: str = "EBAY_DE") -> list[dict]:
    """
    Returns a list of plain dicts (already picked down to the fields the
    frontend needs) for active listings matching `query`. Raises ValueError
    on any upstream failure - the router turns that into an HTTP error.
    """
    token = _get_app_token()
    base_url = EBAY_BROWSE_BASE[settings.ebay_environment]

    resp = httpx.get(
        f"{base_url}/buy/browse/v1/item_summary/search",
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": marketplace,
        },
        params={"q": query, "limit": min(limit, 50)},
        timeout=20.0,
    )
    if resp.status_code != 200:
        raise ValueError(f"ebay_browse_search_failed: {resp.text}")

    items = resp.json().get("itemSummaries", [])

    # Fetch estimated availability/sold quantity for the first ENRICH_LIMIT
    # items in parallel, so total latency stays close to one round-trip
    # instead of one-per-item.
    availabilities: dict[str, dict] = {}
    to_enrich = [item["itemId"] for item in items[:ENRICH_LIMIT] if item.get("itemId")]
    if to_enrich:
        with ThreadPoolExecutor(max_workers=len(to_enrich)) as pool:
            futures = {
                pool.submit(_fetch_availability, item_id, token, marketplace, base_url): item_id
                for item_id in to_enrich
            }
            for future in as_completed(futures):
                availabilities[futures[future]] = future.result()

    results = []
    for item in items:
        availability = availabilities.get(item.get("itemId"), {})
        price = item.get("price", {})
        seller = item.get("seller", {})
        image = item.get("image", {})

        results.append({
            "title": item.get("title"),
            "price": price.get("value"),
            "currency": price.get("currency"),
            "condition": item.get("condition"),
            "image_url": image.get("imageUrl"),
            "item_web_url": item.get("itemWebUrl"),
            "seller_username": seller.get("username"),
            "seller_feedback_percentage": seller.get("feedbackPercentage"),
            "seller_feedback_score": seller.get("feedbackScore"),
            "estimated_available_quantity": availability.get("estimatedAvailableQuantity"),
            "estimated_sold_quantity": availability.get("estimatedSoldQuantity"),
        })
    return results
