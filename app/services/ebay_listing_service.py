"""Imports the seller's active eBay listings as products (no CSV needed).

Uses the Trading API call GetMyeBaySelling, which - unlike the Inventory
API - also returns listings that were created in the eBay web UI / Seller
Hub. Existing products (matched by SKU, else by item number) are never
overwritten: only missing ones are created, with the listing price and
the quantity eBay still has available.
"""

import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from app.models.ebay_account import EbayAccount
from app.models.product import Product
from app.services import ebay_oauth_service

TRADING_URL = {
    "SANDBOX": "https://api.sandbox.ebay.com/ws/api.dll",
    "PRODUCTION": "https://api.ebay.com/ws/api.dll",
}
NS = {"e": "urn:ebay:apis:eBLBaseComponents"}
PAGE_SIZE = 200
MAX_PAGES = 25  # safety cap: 5000 listings


def _request_xml(page: int) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<GetMyeBaySellingRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        "<ActiveList><Include>true</Include>"
        f"<Pagination><EntriesPerPage>{PAGE_SIZE}</EntriesPerPage><PageNumber>{page}</PageNumber></Pagination>"
        "</ActiveList></GetMyeBaySellingRequest>"
    )


def _text(node, path):
    found = node.find(path, NS)
    return found.text.strip() if found is not None and found.text else None


def _int(value, default=0):
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return default


def parse_active_list(xml_text: str) -> tuple[list[dict], int]:
    """Returns (listings, total_pages). Raises ValueError on an eBay error response."""
    root = ET.fromstring(xml_text)
    if _text(root, "e:Ack") in ("Failure",):
        message = _text(root, "e:Errors/e:LongMessage") or _text(root, "e:Errors/e:ShortMessage") or "unknown"
        raise ValueError(f"ebay_get_listings_failed: {message}")

    total_pages = _int(_text(root, "e:ActiveList/e:PaginationResult/e:TotalNumberOfPages"), 1)
    listings = []
    for item in root.findall("e:ActiveList/e:ItemArray/e:Item", NS):
        item_id = _text(item, "e:ItemID")
        if not item_id:
            continue
        try:
            price = Decimal(_text(item, "e:SellingStatus/e:CurrentPrice") or _text(item, "e:BuyItNowPrice") or "")
        except InvalidOperation:
            price = None
        quantity = _int(_text(item, "e:Quantity"))
        sold = _int(_text(item, "e:SellingStatus/e:QuantitySold"))
        listings.append(
            {
                "item_id": item_id,
                "sku": _text(item, "e:SKU"),
                "title": _text(item, "e:Title") or item_id,
                "price": price,
                "available": _int(_text(item, "e:QuantityAvailable"), max(quantity - sold, 0)),
                "image_url": _text(item, "e:PictureDetails/e:GalleryURL"),
            }
        )
    return listings, total_pages


def _headers(token: str, call: str) -> dict:
    return {
        "X-EBAY-API-IAF-TOKEN": token,
        "X-EBAY-API-SITEID": "77",  # eBay Germany
        "X-EBAY-API-COMPATIBILITY-LEVEL": "1193",
        "X-EBAY-API-CALL-NAME": call,
        "Content-Type": "text/xml",
    }


def fetch_active_listings(account: EbayAccount, db: Session) -> list[dict]:
    token = ebay_oauth_service.get_valid_access_token(account, db)
    headers = _headers(token, "GetMyeBaySelling")

    listings, page, total_pages = [], 1, 1
    while page <= min(total_pages, MAX_PAGES):
        resp = httpx.post(
            TRADING_URL[account.environment], headers=headers, content=_request_xml(page), timeout=30.0
        )
        if resp.status_code != 200:
            raise ValueError(f"ebay_get_listings_failed: HTTP {resp.status_code} {resp.text[:200]}")
        page_items, total_pages = parse_active_list(resp.text)
        listings += page_items
        page += 1
    return listings


def _revise_xml(items: list[tuple]) -> str:
    """items: (item_id, price or None, quantity or None); only given fields are sent."""
    body = ""
    for item_id, price, quantity in items:
        fields = ""
        if price is not None:
            fields += f"<StartPrice>{price}</StartPrice>"
        if quantity is not None:
            fields += f"<Quantity>{int(quantity)}</Quantity>"
        body += f"<InventoryStatus><ItemID>{item_id}</ItemID>{fields}</InventoryStatus>"
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<ReviseInventoryStatusRequest xmlns="urn:ebay:apis:eBLBaseComponents">{body}</ReviseInventoryStatusRequest>'
    )


def _revise_call(account: EbayAccount, token: str, items: list[tuple]) -> tuple[bool, str]:
    resp = httpx.post(
        TRADING_URL[account.environment], headers=_headers(token, "ReviseInventoryStatus"),
        content=_revise_xml(items), timeout=30.0,
    )
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    root = ET.fromstring(resp.text)
    if _text(root, "e:Ack") in ("Success", "Warning"):
        return True, ""
    return False, _text(root, "e:Errors/e:LongMessage") or _text(root, "e:Errors/e:ShortMessage") or "unknown"


def revise_listings(account: EbayAccount, db: Session, items: list[tuple]) -> dict[str, str | None]:
    """Sets price and/or available quantity for (item_id, price|None, quantity|None) tuples.
    Returns {item_id: None if ok else error text}. eBay's request Quantity is the AVAILABLE
    quantity (eBay adds the sold units itself). Sends up to 4 items per call (eBay's limit);
    if a call fails, retries one by one so each failure is attributed to the right listing."""
    token = ebay_oauth_service.get_valid_access_token(account, db)
    results: dict[str, str | None] = {}
    for i in range(0, len(items), 4):
        chunk = items[i:i + 4]
        ok, err = _revise_call(account, token, chunk)
        if ok:
            results.update({item[0]: None for item in chunk})
            continue
        for single in chunk:
            ok, err = _revise_call(account, token, [single])
            results[single[0]] = None if ok else err
    return results


def revise_prices(account: EbayAccount, db: Session, items: list[tuple[str, Decimal]]) -> dict[str, str | None]:
    return revise_listings(account, db, [(item_id, price, None) for item_id, price in items])


def import_listings(account: EbayAccount, tenant_id: UUID, db: Session) -> dict:
    listings = fetch_active_listings(account, db)

    existing = {p.sku for p in db.query(Product).filter(Product.tenant_id == tenant_id).all()}
    created = skipped = 0
    for listing in listings:
        sku = listing["sku"] or listing["item_id"]
        # An order line without SKU is stored under its item number, so
        # accept either spelling as "already known".
        if sku in existing or listing["item_id"] in existing:
            skipped += 1
            continue
        db.add(
            Product(
                tenant_id=tenant_id,
                sku=sku,
                title=listing["title"][:255],
                selling_price=listing["price"],
                stock_quantity=listing["available"],
                image_url=listing["image_url"],
            )
        )
        existing.add(sku)
        created += 1
    db.commit()
    return {"listings_found": len(listings), "products_created": created, "already_known": skipped}
