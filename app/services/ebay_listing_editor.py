"""Edits one live eBay listing from NettoDesk: pictures, and price / quantity per
variation (colour, size ...). Everything goes into ONE revise call, so eBay takes
all of it or none of it.

eBay rules this relies on (Trading API reference, ReviseFixedPriceItem):
- Variation.Quantity in a revise request is the AVAILABLE quantity; eBay adds the
  units already sold itself. (GetItem returns available + sold.)
- A changed variation is sent with its VariationSpecifics and StartPrice.
- PictureDetails replaces the whole gallery (max 24), Variations.Pictures replaces
  all variation picture sets (max 12 per value).
- Pictures hosted by eBay and elsewhere cannot be mixed, so every new picture is
  first copied to eBay Picture Services (Media API createImageFromUrl).
"""

import xml.etree.ElementTree as ET
from decimal import Decimal
from urllib.parse import urlparse
from xml.sax.saxutils import escape

import httpx
from sqlalchemy.orm import Session

from app.models.ebay_account import EbayAccount
from app.services import ebay_listing_service, ebay_oauth_service

MEDIA_URL = {
    "SANDBOX": "https://apim.sandbox.ebay.com/commerce/media/v1_beta/image",
    "PRODUCTION": "https://apim.ebay.com/commerce/media/v1_beta/image",
}
MAX_GALLERY = 24
MAX_PER_VARIATION = 12
RELOAD = "Das Angebot hat sich inzwischen geändert, bitte den Editor neu öffnen."


def available(variation: dict) -> int:
    """Also works for the listing itself: both carry quantity (incl. sold) and sold."""
    return max(0, int(variation.get("quantity") or 0) - int(variation.get("sold") or 0))


def listing_view(info: dict) -> dict:
    """What the editor shows: the same data with the AVAILABLE quantity worked out."""
    variations = [{**v, "available": available(v)} for v in info["variations"]]
    names: dict[str, list[str]] = {}
    for v in info["variations"]:
        for name, value in v["specifics"].items():
            if value not in names.setdefault(name, []):
                names[name].append(value)
    return {**info, "variations": variations, "specific_values": names, "available": available(info)}


def _is_ebay_hosted(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "ebayimg.com" or host.endswith(".ebayimg.com")


def host_on_ebay(environment: str, token: str, url: str) -> str:
    """Returns an eBay-hosted address for this picture, copying it there if needed."""
    if _is_ebay_hosted(url):
        return url
    if urlparse(url).scheme != "https":
        raise ValueError("Bilder brauchen eine https-Adresse.")
    auth = {"Authorization": f"Bearer {token}"}
    resp = httpx.post(f"{MEDIA_URL[environment]}/create_image_from_url",
                      headers=auth, json={"imageUrl": url}, timeout=60.0)
    if resp.status_code not in (200, 201):
        raise ValueError(f"eBay hat ein Bild nicht angenommen (HTTP {resp.status_code}): {resp.text[:300]}")
    hosted = _image_url(resp)
    location = resp.headers.get("Location")
    if not hosted and location:
        # The token is only ever sent back to eBay's own media host.
        if urlparse(location).hostname != urlparse(MEDIA_URL[environment]).hostname:
            raise ValueError("eBay hat eine unerwartete Bildadresse geliefert.")
        hosted = _image_url(httpx.get(location, headers=auth, timeout=30.0))
    if not hosted:
        raise ValueError("eBay hat keine Bildadresse zurückgegeben.")
    return hosted


def _image_url(resp) -> str | None:
    try:
        return resp.json().get("imageUrl")
    except ValueError:
        return None


def _key(sku, specifics: dict) -> tuple:
    return (sku or "", tuple(sorted((specifics or {}).items())))


def _specifics_xml(specifics: dict) -> str:
    return "<VariationSpecifics>" + "".join(
        f"<NameValueList><Name>{escape(n)}</Name><Value>{escape(v)}</Value></NameValueList>"
        for n, v in specifics.items()
    ) + "</VariationSpecifics>"


def _picture_urls_xml(urls: list[str]) -> str:
    return "".join(f"<PictureURL>{escape(u)}</PictureURL>" for u in urls)


def plan(info: dict, changes: list[dict], pictures, picture_name, picture_sets) -> list[tuple]:
    """Checks the requested changes against what eBay has now. Returns
    [(current variation, new price or None, new quantity or None)]. Raises ValueError."""
    current = {_key(v.get("sku"), v["specifics"]): v for v in info["variations"]}
    todo = []
    for change in changes:
        variation = current.get(_key(change.get("sku"), change.get("specifics")))
        if variation is None:
            raise ValueError(RELOAD)
        price, quantity = change.get("price"), change.get("quantity")
        if price is None and quantity is None:
            continue
        if price is not None and price <= 0:
            raise ValueError("Der Preis muss größer als 0 sein.")
        if quantity is not None:
            if quantity < 0:
                raise ValueError("Die Menge darf nicht negativ sein.")
            seen = change.get("quantity_seen")
            if seen is not None and seen != available(variation):
                raise ValueError("Die Menge hat sich bei eBay inzwischen geändert (Verkauf?). "
                                 "Bitte den Editor neu öffnen.")
        todo.append((variation, price, quantity))

    if pictures is not None:
        if not pictures:
            raise ValueError("Das Angebot braucht mindestens ein Bild.")
        if len(pictures) > MAX_GALLERY:
            raise ValueError(f"Höchstens {MAX_GALLERY} Bilder pro Angebot.")
    if picture_sets is not None:
        values = {v["specifics"].get(picture_name) for v in info["variations"]} - {None}
        if not picture_name or not values:
            raise ValueError(RELOAD)
        if info.get("picture_sets") and info.get("picture_name") != picture_name:
            raise ValueError(f"Die Variantenbilder gehören zum Merkmal „{info.get('picture_name')}“.")
        if set(picture_sets) - values:
            raise ValueError(RELOAD)
        if any(len(urls) > MAX_PER_VARIATION for urls in picture_sets.values()):
            raise ValueError(f"Höchstens {MAX_PER_VARIATION} Bilder pro Variante.")
        if not any(picture_sets.values()):
            raise ValueError("Mindestens eine Variante muss ein Bild behalten.")
    return todo


def build_xml(item_id: str, todo: list[tuple], pictures, picture_name, picture_sets) -> str:
    body = f"<ItemID>{escape(item_id)}</ItemID>"
    if pictures is not None:
        body += f"<PictureDetails>{_picture_urls_xml(pictures)}</PictureDetails>"
    inner = ""
    for variation, price, quantity in todo:
        part = f"<SKU>{escape(variation['sku'])}</SKU>" if variation.get("sku") else ""
        part += f"<StartPrice>{price if price is not None else variation['price']}</StartPrice>"
        if quantity is None and available(variation) > 0:
            quantity = available(variation)
        # A sold-out variation whose quantity is not being changed is sent without one:
        # sending 0 can make eBay drop the variation.
        if quantity is not None:
            part += f"<Quantity>{int(quantity)}</Quantity>"
        inner += f"<Variation>{part}{_specifics_xml(variation['specifics'])}</Variation>"
    if picture_sets is not None:
        sets = "".join(
            f"<VariationSpecificPictureSet><VariationSpecificValue>{escape(value)}</VariationSpecificValue>"
            f"{_picture_urls_xml(urls)}</VariationSpecificPictureSet>"
            for value, urls in picture_sets.items() if urls
        )
        inner += f"<Pictures><VariationSpecificName>{escape(picture_name)}</VariationSpecificName>{sets}</Pictures>"
    if inner:
        body += f"<Variations>{inner}</Variations>"
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"<Item>{body}</Item></ReviseFixedPriceItemRequest>"
    )


def apply(account: EbayAccount, db: Session, item_id: str, info: dict, changes: list[dict],
          pictures: list[str] | None, picture_name: str | None,
          picture_sets: dict[str, list[str]] | None) -> dict:
    """info must be a fresh get_item_variations() answer. Returns what was sent."""
    todo = plan(info, changes, pictures, picture_name, picture_sets)
    if not todo and pictures is None and picture_sets is None:
        raise ValueError("Es wurde nichts geändert.")
    token = ebay_oauth_service.get_valid_access_token(account, db)

    hosted: dict[str, str] = {}

    def on_ebay(urls):
        for url in urls:
            if url not in hosted:
                hosted[url] = host_on_ebay(account.environment, token, url)
        return [hosted[url] for url in urls]

    if pictures is not None:
        pictures = on_ebay(pictures)
    if picture_sets is not None:
        picture_sets = {value: on_ebay(urls) for value, urls in picture_sets.items()}

    resp = httpx.post(
        ebay_listing_service.TRADING_URL[account.environment],
        headers=ebay_listing_service._headers(token, "ReviseFixedPriceItem"),
        content=build_xml(item_id, todo, pictures, picture_name, picture_sets).encode("utf-8"), timeout=60.0,
    )
    if resp.status_code != 200:
        raise ValueError(f"HTTP {resp.status_code}")
    root = ET.fromstring(resp.text)
    text = ebay_listing_service._text
    if text(root, "e:Ack") not in ("Success", "Warning"):
        errors = [text(e, "e:LongMessage") or text(e, "e:ShortMessage")
                  for e in root.findall("e:Errors", ebay_listing_service.NS)]
        raise ValueError(" ".join(e for e in errors if e) or "unknown")
    return {"todo": todo, "pictures": pictures}


def totals(info: dict, todo: list[tuple]) -> tuple[int, Decimal | None]:
    """(sum of available quantities, lowest price) after the changes in todo."""
    new = {_key(v.get("sku"), v["specifics"]): (price, qty) for v, price, qty in todo}
    quantity, prices = 0, []
    for v in info["variations"]:
        price, qty = new.get(_key(v.get("sku"), v["specifics"]), (None, None))
        quantity += available(v) if qty is None else qty
        if price is not None or v.get("price"):
            prices.append(Decimal(str(price if price is not None else v["price"])))
    return quantity, min(prices) if prices else None
