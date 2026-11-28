"""
Letter products that can be bought: product code, price and size limits.

The table (internetmarke_ppl600.json) was generated from Deutsche Post's partner
price list 6.00 ("PPL60_EPORTO_komplett_20260513", developer.dhl.com,
INTERNETMARKE downloads): all 51 products, valid until 31.12.2026. Checkout must
send the exact current price, so after that day nothing is offered until the next
list is entered - an old price is never sent. Kompaktbrief is listed first and
Großbrief second; the first domestic one is preselected when buying.
"""

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Product:
    code: int
    name: str
    price_cents: int
    valid_from: date
    valid_to: date
    international: bool = False
    tracking: bool = False
    contract_required: bool = False
    min_length_mm: int | None = None
    max_length_mm: int | None = None
    min_width_mm: int | None = None
    max_width_mm: int | None = None
    max_height_mm: int | None = None
    min_weight_g: int | None = None
    max_weight_g: int | None = None
    note: str = ""


def _load() -> tuple[Product, ...]:
    rows = json.loads(Path(__file__).with_name("internetmarke_ppl600.json").read_text(encoding="utf-8"))
    return tuple(Product(**{**row, "valid_from": date.fromisoformat(row["valid_from"]),
                            "valid_to": date.fromisoformat(row["valid_to"])}) for row in rows)


PRODUCTS: tuple[Product, ...] = _load()


def available(today: date | None = None) -> list[Product]:
    today = today or datetime.now(ZoneInfo("Europe/Berlin")).date()
    return [p for p in PRODUCTS if p.valid_from <= today <= p.valid_to]
