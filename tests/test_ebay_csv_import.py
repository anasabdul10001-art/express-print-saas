"""eBay 'Active listings' CSV import: parsing (DE/EN headers, price formats) and the upsert endpoint."""

import pytest

from app.services.ebay_csv_import import CsvImportError, parse_active_listings_csv, parse_price

DE_CSV = (
    "﻿Artikelnummer;Titel;Eigene Artikelnummer;Verfügbare Menge;Format;Sofort-Kaufen-Preis;Artikelfoto-URL\n"
    "1001;USB-C Kabel 1m;CAB-1;12;Festpreis;2,99;https://i.ebayimg.com/a.jpg\n"
    "1002;AUX Adapter;;3;Festpreis;3,49;\n"
    "1003;Auktionsding;AUC-1;1;Auktion;1,00;\n"
).encode("utf-8")


@pytest.mark.parametrize("raw,expected", [
    ("2,99", "2.99"), ("2.99", "2.99"), ("EUR 2,99", "2.99"), ("1.234,56", "1234.56"),
    ("1,234.56", "1234.56"), ("", None), (None, None), ("abc", None),
])
def test_parse_price(raw, expected):
    result = parse_price(raw)
    assert (str(result) if result is not None else None) == expected


def test_parse_german_csv_with_fallbacks():
    parsed = parse_active_listings_csv(DE_CSV)
    skus = [p["sku"] for p in parsed["products"]]
    assert skus == ["CAB-1", "1002"]  # second row falls back to the eBay item number
    assert str(parsed["products"][0]["selling_price"]) == "2.99"
    assert parsed["products"][0]["stock_quantity"] == 12
    assert parsed["skipped"] == 1  # the auction
    assert any("Auktion" in w for w in parsed["warnings"])


def test_parse_english_comma_csv_with_note_lines_above_header():
    content = (
        "Some note from eBay\n"
        "Item number,Title,Custom label (SKU),Available quantity,Start price\n"
        '2001,"Cable, white",W-1,5,"4.50"\n'
    ).encode()
    parsed = parse_active_listings_csv(content)
    assert parsed["products"][0]["title"] == "Cable, white"
    assert str(parsed["products"][0]["selling_price"]) == "4.50"


def test_unusable_file_is_rejected():
    with pytest.raises(CsvImportError):
        parse_active_listings_csv(b"foo,bar\n1,2\n")


def _upload(client, headers, content, dry_run):
    return client.post(
        f"/products/import-ebay-csv?dry_run={'true' if dry_run else 'false'}",
        headers=headers, files={"file": ("active.csv", content, "text/csv")},
    )


def test_import_dry_run_writes_nothing(client, register):
    headers, _ = register()
    body = _upload(client, headers, DE_CSV, dry_run=True).json()
    assert body["dry_run"] is True and body["created"] == 2
    assert client.get("/products", headers=headers).json() == []


def test_import_creates_then_updates_without_touching_purchase_price(client, register):
    headers, _ = register()
    assert _upload(client, headers, DE_CSV, dry_run=False).json()["created"] == 2
    products = {p["sku"]: p for p in client.get("/products", headers=headers).json()}
    assert products["CAB-1"]["stock_quantity"] == 12

    client.patch(f"/products/{products['CAB-1']['id']}", headers=headers, json={"purchase_price": 1.5})
    changed = DE_CSV.replace(b";12;", b";9;").replace(b"2,99", b"3,19")
    result = _upload(client, headers, changed, dry_run=False).json()
    assert result["created"] == 0 and result["updated"] == 1 and result["unchanged"] == 1

    cab = client.get(f"/products/{products['CAB-1']['id']}", headers=headers).json()
    assert cab["stock_quantity"] == 9
    assert float(cab["selling_price"]) == 3.19
    assert float(cab["purchase_price"]) == 1.5  # never overwritten by the import
    movements = client.get(f"/products/{cab['id']}/movements", headers=headers).json()
    assert sorted(m["quantity_change"] for m in movements) == [-3, 12]


def test_import_rejects_garbage_and_requires_login(client, register):
    headers, _ = register()
    assert _upload(client, headers, b"foo,bar\n1,2\n", dry_run=True).status_code == 422
    assert client.post("/products/import-ebay-csv", files={"file": ("a.csv", b"x", "text/csv")}).status_code in (401, 403)
