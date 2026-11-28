"""Imports the seller's active eBay listings as products (no CSV needed).

Uses the Trading API call GetMyeBaySelling, which - unlike the Inventory
API - also returns listings that were created in the eBay web UI / Seller
Hub. Existing products (matched by SKU, else by item number) are never
overwritten (only a missing picture is filled in): only missing ones are
created, with the listing price and the quantity eBay still has available.
"""

import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
from decimal import Decimal, InvalidOperation
from uuid import UUID

import httpx
from sqlalchemy.orm import Session

from app.models.ebay_account import EbayAccount
from app.models.product import Product
from app.services import stock_listing_service
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


def get_item_picture(account: EbayAccount, token: str, item_id: str) -> str | None:
    """Read-only GetItem: the picture of one listing. Also answers for ended or sold-out
    listings (for a while), which are no longer part of the active list."""
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"<ItemID>{escape(item_id)}</ItemID><DetailLevel>ReturnAll</DetailLevel></GetItemRequest>"
    )
    resp = httpx.post(TRADING_URL[account.environment], headers=_headers(token, "GetItem"), content=xml, timeout=15.0)
    if resp.status_code != 200:
        return None
    root = ET.fromstring(resp.text)
    return (_text(root, "e:Item/e:PictureDetails/e:GalleryURL")
            or _text(root, "e:Item/e:PictureDetails/e:PictureURL"))


def _urls(node, path) -> list[str]:
    return [n.text.strip() for n in node.findall(path, NS) if n.text]


def get_item_variations(account: EbayAccount, db: Session, item_id: str) -> dict:
    """Read-only GetItem: the variations (SKU, price, available quantity, specifics) of one listing."""
    token = ebay_oauth_service.get_valid_access_token(account, db)
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"<ItemID>{item_id}</ItemID><DetailLevel>ReturnAll</DetailLevel></GetItemRequest>"
    )
    resp = httpx.post(TRADING_URL[account.environment], headers=_headers(token, "GetItem"), content=xml, timeout=30.0)
    if resp.status_code != 200:
        raise ValueError(f"HTTP {resp.status_code}")
    root = ET.fromstring(resp.text)
    if _text(root, "e:Ack") == "Failure":
        raise ValueError(_text(root, "e:Errors/e:LongMessage") or "unknown")
    item = root.find("e:Item", NS)
    out = {
        "item_id": item_id,
        "item_sku": _text(item, "e:SKU"),
        "price": _text(item, "e:SellingStatus/e:CurrentPrice"),
        "quantity": _text(item, "e:Quantity"),
        "sold": _text(item, "e:SellingStatus/e:QuantitySold"),
        "listing_type": _text(item, "e:ListingType"),
        "title": _text(item, "e:Title"),
        "pictures": _urls(item, "e:PictureDetails/e:PictureURL"),
        "picture_name": _text(item, "e:Variations/e:Pictures/e:VariationSpecificName"),
        "picture_sets": {
            _text(ps, "e:VariationSpecificValue"): _urls(ps, "e:PictureURL")
            for ps in item.findall("e:Variations/e:Pictures/e:VariationSpecificPictureSet", NS)
        },
        "variations": [],
    }
    for v in item.findall("e:Variations/e:Variation", NS):
        out["variations"].append({
            "sku": _text(v, "e:SKU"),
            "price": _text(v, "e:StartPrice"),
            "quantity": _text(v, "e:Quantity"),
            "sold": _text(v, "e:SellingStatus/e:QuantitySold"),
            "specifics": {
                _text(n, "e:Name"): _text(n, "e:Value")
                for n in v.findall("e:VariationSpecifics/e:NameValueList", NS)
            },
        })
    return out


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


def _variation_price_xml(item_id: str, price: Decimal, variations: list[dict]) -> str:
    """ReviseFixedPriceItem body that sets one price on every variation (identified by SKU or specifics)."""
    from xml.sax.saxutils import escape

    parts = ""
    for v in variations:
        if v.get("sku"):
            ident = f"<SKU>{escape(v['sku'])}</SKU>"
        else:
            ident = "<VariationSpecifics>" + "".join(
                f"<NameValueList><Name>{escape(n)}</Name><Value>{escape(val)}</Value></NameValueList>"
                for n, val in v["specifics"].items()
            ) + "</VariationSpecifics>"
        parts += f"<Variation>{ident}<StartPrice>{price}</StartPrice></Variation>"
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"<Item><ItemID>{item_id}</ItemID><Variations>{parts}</Variations></Item></ReviseFixedPriceItemRequest>"
    )


def _revise_variation_listing(account: EbayAccount, db: Session, token: str, item: tuple) -> str | None:
    """Fallback for listings with variations (e.g. colours): price goes to every variation.
    Quantity cannot be set from one total, so it is refused with a clear message.
    Returns None if the listing has no variations (caller keeps the original error) via
    the sentinel "NO_VARIATIONS"."""
    item_id, price, quantity = item
    info = get_item_variations(account, db, item_id)
    variations = info["variations"]
    if not variations:
        return "NO_VARIATIONS"
    if price is None:
        return "Mehrere Varianten (z. B. Farben): Menge bitte pro Variante im Angebots-Editor ändern."
    if len({v.get("price") for v in variations}) > 1:
        # one price for all would silently wipe e.g. the different prices per size
        return "Die Varianten haben unterschiedliche Preise: bitte im Angebots-Editor ändern."
    resp = httpx.post(
        TRADING_URL[account.environment], headers=_headers(token, "ReviseFixedPriceItem"),
        content=_variation_price_xml(item_id, price, variations), timeout=30.0,
    )
    if resp.status_code != 200:
        return f"HTTP {resp.status_code}"
    root = ET.fromstring(resp.text)
    if _text(root, "e:Ack") not in ("Success", "Warning"):
        return _text(root, "e:Errors/e:LongMessage") or _text(root, "e:Errors/e:ShortMessage") or "unknown"
    if quantity is not None:
        return "Preis bei allen Varianten geändert; Menge nicht (Varianten: bitte pro Variante im Angebots-Editor ändern)."
    return None


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
            if not ok:
                try:
                    variant_err = _revise_variation_listing(account, db, token, single)
                except (ValueError, httpx.HTTPError, ET.ParseError):
                    variant_err = "NO_VARIATIONS"
                if variant_err != "NO_VARIATIONS":
                    results[single[0]] = variant_err
                    continue
            results[single[0]] = None if ok else err
    return results


def revise_prices(account: EbayAccount, db: Session, items: list[tuple[str, Decimal]]) -> dict[str, str | None]:
    return revise_listings(account, db, [(item_id, price, None) for item_id, price in items])


def import_listings(account: EbayAccount, tenant_id: UUID, db: Session,
                    listings: list[dict] | None = None) -> dict:
    if listings is None:
        listings = fetch_active_listings(account, db)

    stock_linked = stock_listing_service.seen_in_shop(db, tenant_id, [
        (key, listing["image_url"], listing["price"])
        for listing in listings for key in (listing["sku"], listing["item_id"]) if key])
    products = {p.sku: p for p in db.query(Product).filter(Product.tenant_id == tenant_id).all()}
    existing = set(products)
    created = skipped = 0
    for listing in listings:
        sku = listing["sku"] or listing["item_id"]
        # An order line without SKU is stored under its item number, so
        # accept either spelling as "already known".
        if sku in existing or listing["item_id"] in existing:
            # A missing picture is the one thing filled in (e.g. a product the order
            # sync created: order lines carry no picture).
            known = products.get(sku) or products.get(listing["item_id"])
            if known and not known.image_url:
                known.image_url = listing["image_url"]
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
    return {"listings_found": len(listings), "products_created": created, "already_known": skipped,
            "stock_linked": stock_linked}
