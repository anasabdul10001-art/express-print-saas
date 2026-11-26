"""Links between the names on supplier invoices and the shop's products.

A product is chosen automatically only for an item that was linked before (same
supplier and same item number, or exactly the same name). Similar names are only
offered as suggestions: another colour or cable length is another product.
"""

import re
import unicodedata
from difflib import SequenceMatcher

from sqlalchemy.orm import Session

from app.models.product import Product
from app.models.supplier_item import SupplierItem

MAX_SUGGESTIONS = 3
MIN_SCORE = 0.3


def normalize(text: str | None) -> str:
    """Case and spacing do not matter; every other character does ("1.5m" is not "15m")."""
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


def _match_value(name: str, code: str) -> str | None:
    if normalize(code):
        return f"code:{normalize(code)}"[:520]
    if normalize(name):
        return f"name:{normalize(name)}"[:520]
    return None


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", normalize(text)))


def _score(name: str, code: str, product) -> float:
    if normalize(code) and normalize(code) == normalize(product.sku):
        return 1.0
    if not normalize(name):
        return 0.0
    ours, theirs = _words(product.title), _words(name)
    shared = len(ours & theirs) / len(ours | theirs) if ours | theirs else 0.0
    spelling = SequenceMatcher(None, normalize(name), normalize(product.title)).ratio()
    return 0.7 * shared + 0.3 * spelling


def remembered(rows: list, name: str, code: str):
    """The saved link for one invoice item, or None. `rows` are one supplier's links."""
    by_value = {row.match_value: row for row in rows}
    if normalize(code):
        # a new item number is a new item, unless this very name was linked without any number
        return by_value.get(_match_value("", code)) or by_value.get(_match_value(name, ""))
    if not normalize(name):
        return None
    same = [row for row in rows if normalize(row.item_name) == normalize(name)]
    targets = {(row.product_id, row.units_per_pack) for row in same}
    return same[0] if len(targets) == 1 else None


def match(db: Session, tenant_id, supplier: str, items: list[tuple[str, str]]) -> list[dict]:
    """For each (name, code): the remembered product, otherwise suggestions."""
    products = db.query(Product.id, Product.title, Product.sku, Product.image_url).filter(
        Product.tenant_id == tenant_id, Product.active.is_(True)).all()
    active = {p.id for p in products}
    rows = [row for row in db.query(SupplierItem).filter(
        SupplierItem.tenant_id == tenant_id, SupplierItem.supplier_norm == normalize(supplier))
        if row.product_id in active]
    result = []
    for name, code in items:
        row = remembered(rows, name, code)
        if row:
            result.append({"match": {"product_id": str(row.product_id), "units_per_pack": row.units_per_pack},
                           "suggestions": []})
            continue
        scored = sorted(((_score(name, code, p), p) for p in products), key=lambda s: -s[0])
        # suppliers often write in another language: two shared words ("usb", "1m") are enough to offer it
        scored = [(score, p) for score, p in scored
                  if score >= MIN_SCORE or len(_words(name) & _words(p.title)) >= 2]
        result.append({"match": None, "suggestions": [
            {"product_id": str(p.id), "title": p.title, "image_url": p.image_url}
            for score, p in scored[:MAX_SUGGESTIONS]]})
    return result


def remember(db: Session, tenant_id, supplier: str, items: list) -> None:
    """Saves the links of a booked invoice; a later choice replaces an earlier one.
    One name or number pointing to two products on the same invoice is ambiguous
    and is forgotten rather than guessed."""
    supplier_norm = normalize(supplier)[:255]
    chosen: dict[str, list] = {}
    for item in items:
        value = _match_value(item.name, item.code)
        if value:
            chosen.setdefault(value, []).append(item)
    for value, same in chosen.items():
        row = db.query(SupplierItem).filter(
            SupplierItem.tenant_id == tenant_id, SupplierItem.supplier_norm == supplier_norm,
            SupplierItem.match_value == value).first()
        if len({(i.product_id, i.units_per_pack) for i in same}) > 1:
            if row:
                db.delete(row)
            continue
        item = same[0]
        if row is None:
            row = SupplierItem(tenant_id=tenant_id, supplier_norm=supplier_norm, match_value=value)
            db.add(row)
        row.supplier = supplier.strip()[:255]
        row.item_name, row.item_code = item.name.strip(), item.code.strip()
        row.product_id, row.units_per_pack = item.product_id, item.units_per_pack
