"""
Parses the "Active listings" CSV a seller downloads from eBay Seller Hub
(Listings -> Active -> Download) into plain product dicts.

Why a CSV and not the API: eBay's Inventory API does not return listings
that were created in Seller Hub unless they are migrated first
(bulkMigrateListing), which modifies the live listings. Reading a CSV the
seller downloads himself is read-only on eBay's side and needs no API
approval.

IMPORTANT - format caveat: eBay does not publish a fixed schema for this
file and the column names differ by site language (German vs English UI)
and over time. So columns are matched by normalised name against synonym
lists (German + English) rather than by position. The matching was written
from eBay's documented column names and is covered by synthetic test files;
it has NOT yet been checked against a real file downloaded from the user's
eBay.de account. Unknown columns are ignored; if the title column cannot be
found the import fails loudly instead of guessing.
"""

import csv
import io
import re
import unicodedata
from decimal import Decimal, InvalidOperation

MAX_ROWS = 5000

_FIELD_SYNONYMS = {
    "item_number": {"itemnumber", "itemid", "artikelnummer", "artikelnr"},
    "title": {"title", "titel", "artikelbezeichnung", "artikeltitel", "bezeichnung"},
    "sku": {
        "customlabel", "customlabelsku", "sku", "eigeneartikelnummer",
        "eigeneartikelnummersku", "benutzerdefiniertesetikett", "benutzerdefiniertesetikettsku",
    },
    "quantity": {"availablequantity", "quantity", "verfugbaremenge", "menge", "anzahl", "verfugbareanzahl"},
    "price": {
        "startprice", "startpreis", "currentprice", "aktuellerpreis", "price", "preis",
        "buyitnowprice", "sofortkaufenpreis", "auctionbuyitnowprice", "festpreis",
    },
    "image_url": {"itemphotourl", "artikelfotourl", "photourl", "imageurl", "bildurl", "galleryurl"},
    "format": {"format", "angebotsformat", "listingformat"},
}


class CsvImportError(ValueError):
    """The file cannot be interpreted as an eBay active-listings export."""


def _norm(header: str) -> str:
    text = unicodedata.normalize("NFKD", header or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _map_headers(cells: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, cell in enumerate(cells):
        key = _norm(cell)
        for field, synonyms in _FIELD_SYNONYMS.items():
            if field not in mapping and key in synonyms:
                mapping[field] = index
    return mapping


def parse_price(raw: str | None) -> Decimal | None:
    """'2,99', '2.99', 'EUR 2,99', '1.234,56' and '1,234.56' all work; anything else -> None."""
    if raw is None:
        return None
    cleaned = re.sub(r"[^0-9.,]", "", raw)
    if not cleaned:
        return None
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return value if value >= 0 else None


def _parse_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    digits = re.sub(r"[^0-9]", "", raw)
    return int(digits) if digits else None


def parse_active_listings_csv(content: bytes) -> dict:
    """
    Returns {"products": [...], "warnings": [...], "skipped": int}.
    Each product: sku, title, selling_price, stock_quantity, image_url
    (price/quantity/image may be None when the column is missing or empty).
    Raises CsvImportError when the file is not usable.
    """
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1252", errors="replace")

    lines = text.splitlines()
    header_index = None
    delimiter = ","
    mapping: dict[str, int] = {}
    for index, line in enumerate(lines[:30]):  # eBay sometimes puts notes above the header row
        for candidate in (",", ";", "\t"):
            cells = next(csv.reader([line], delimiter=candidate), [])
            found = _map_headers(cells)
            if "title" in found and len(found) >= 2 and len(found) > len(mapping):
                header_index, delimiter, mapping = index, candidate, found
    if header_index is None:
        raise CsvImportError(
            "Keine Titel-Spalte gefunden - ist das der eBay-Download 'Aktive Angebote' (CSV)?"
        )

    products: list[dict] = []
    warnings: list[str] = []
    skipped = 0
    seen_skus: set[str] = set()
    reader = csv.reader(io.StringIO("\n".join(lines[header_index + 1:])), delimiter=delimiter)

    def cell(row: list[str], field: str) -> str | None:
        position = mapping.get(field)
        if position is None or position >= len(row):
            return None
        value = row[position].strip()
        return value or None

    for row_number, row in enumerate(reader, start=header_index + 2):
        if not any(c.strip() for c in row):
            continue
        if len(products) + skipped >= MAX_ROWS:
            warnings.append(f"Mehr als {MAX_ROWS} Zeilen - der Rest wurde ignoriert.")
            break

        title = cell(row, "title")
        if not title:
            skipped += 1
            continue

        listing_format = (cell(row, "format") or "").lower()
        if "auction" in listing_format or "auktion" in listing_format:
            skipped += 1
            warnings.append(f"Zeile {row_number}: Auktion übersprungen ({title[:40]}).")
            continue

        sku = cell(row, "sku")
        if not sku:
            sku = cell(row, "item_number")
            if sku:
                warnings.append(f"Zeile {row_number}: keine eigene Artikelnummer, eBay-Nummer {sku} als SKU benutzt.")
        if not sku:
            skipped += 1
            warnings.append(f"Zeile {row_number}: weder SKU noch Artikelnummer - übersprungen.")
            continue
        sku = sku[:100]
        if sku in seen_skus:
            skipped += 1
            warnings.append(f"Zeile {row_number}: SKU {sku} kommt doppelt vor - übersprungen.")
            continue
        seen_skus.add(sku)

        products.append({
            "sku": sku,
            "title": title[:500],
            "selling_price": parse_price(cell(row, "price")),
            "stock_quantity": _parse_int(cell(row, "quantity")),
            "image_url": (cell(row, "image_url") or None),
        })

    return {"products": products, "warnings": warnings, "skipped": skipped}
