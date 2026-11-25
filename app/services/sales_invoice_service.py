"""
Builds the snapshot of a seller's invoice from an order and renders it as a PDF
(fpdf2, pure Python). Covers the usual German invoice fields (§ 14 UStG) for
domestic sales: either Kleinunternehmer (§ 19 UStG) or 19 % VAT included in the
price. Reduced rates, EU one-stop-shop and reverse charge are not handled.
"""

import unicodedata
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from fpdf import FPDF

from app.services import order_money

BERLIN = ZoneInfo("Europe/Berlin")
VAT_RATE = Decimal("19")
CENT = Decimal("0.01")
# Above this gross amount the buyer's name and address are mandatory (§ 33 UStDV).
SMALL_INVOICE_LIMIT = Decimal("250")
# The built-in PDF fonts only know Windows-1252. These letters don't decompose.
_TRANSLIT = str.maketrans({"Ł": "L", "ł": "l", "Đ": "D", "đ": "d", "ı": "i", "Ħ": "H", "ħ": "h"})


class InvoiceError(ValueError):
    """The invoice can't be issued; the message is shown to the seller."""


def pdf_text(value: str) -> str:
    """Text the PDF font can print. Accents are simplified if needed; other scripts are refused
    rather than printed as question marks on a legal document."""
    out = []
    for char in value or "":
        try:
            char.encode("cp1252")
            out.append(char)
            continue
        except UnicodeEncodeError:
            pass
        simple = unicodedata.normalize("NFKD", char.translate(_TRANSLIT)).encode("ascii", "ignore").decode()
        if not simple and not unicodedata.combining(char):
            raise InvoiceError(f"Das Zeichen '{char}' kann nicht auf die Rechnung gedruckt werden.")
        out.append(simple)
    return "".join(out)


def _money(value: Decimal) -> str:
    return str(value.quantize(CENT))


def split_vat(gross: Decimal) -> tuple[Decimal, Decimal]:
    """(net, vat) of a gross amount at 19 %; the two always add up to the gross amount."""
    net = (gross * 100 / (100 + VAT_RATE)).quantize(CENT)
    return net, gross - net


def _buyer_lines(order) -> list[str]:
    # Only a complete address counts (§ 14 (4) Nr. 1 UStG: full name and full address).
    if not all((order.recipient_name, order.recipient_street1, order.recipient_zip, order.recipient_city)):
        return []
    city = f"{order.recipient_zip} {order.recipient_city}"
    country = order.recipient_country_code if order.recipient_country_code not in (None, "DE") else None
    return [p for p in (order.recipient_name, order.recipient_street1, order.recipient_street2, city, country) if p]


def build_snapshot(order, profile, vat_mode: str, delivery_date: date) -> dict:
    if not (profile and profile.legal_name and profile.address and (profile.tax_number or profile.vat_id)):
        raise InvoiceError("Bitte zuerst die Rechnungsdaten ausfüllen (Name, Anschrift, Steuernummer oder USt-IdNr.).")
    if order.status == "canceled":
        raise InvoiceError("Für eine stornierte Bestellung kann keine Rechnung erstellt werden.")
    if order.recipient_country_code not in (None, "", "DE"):
        raise InvoiceError("Rechnungen werden bisher nur für Lieferungen innerhalb Deutschlands unterstützt.")

    lines = []
    for item in order.items:
        if item.unit_price is None:
            raise InvoiceError("Mindestens eine Position hat keinen Verkaufspreis.")
        lines.append({
            "title": item.product.title if item.product else "Artikel",
            "quantity": item.quantity,
            "unit_price": _money(item.unit_price),
            "total": _money(item.unit_price * item.quantity),
        })
    if not lines:
        raise InvoiceError("Die Bestellung hat keine Positionen.")

    gross = order_money.revenue(order).quantize(CENT)
    # eBay only tells us what the buyer paid in total, so shipping and discounts appear as one line.
    difference = gross - sum(Decimal(line["total"]) for line in lines)
    if difference:
        label = "Versandkosten (abzgl. etwaiger Rabatte)" if difference > 0 else "Rabatt (inkl. etwaiger Versandkosten)"
        lines.append({"title": label, "quantity": 1, "unit_price": _money(difference), "total": _money(difference)})
    if gross <= 0:
        raise InvoiceError("Der Rechnungsbetrag muss größer als 0 sein.")

    buyer = _buyer_lines(order)
    if not buyer and gross > SMALL_INVOICE_LIMIT:
        raise InvoiceError("Über 250 € braucht die Rechnung Name und Anschrift des Käufers.")

    if order.ebay_payment_status in ("PAID", "PARTIALLY_REFUNDED", "FULLY_REFUNDED"):
        payment_terms = "Der Rechnungsbetrag wurde bereits bezahlt."
    elif order.ebay_account_id or order.shopify_account_id or order.amazon_account_id:
        payment_terms = "Zahlung über den Verkaufsplatz, über den bestellt wurde."
    else:
        payment_terms = "Zahlbar sofort ohne Abzug."

    snapshot = {
        "seller": {
            "name": profile.legal_name, "address": profile.address, "tax_number": profile.tax_number,
            "vat_id": profile.vat_id, "email": profile.email,
        },
        "buyer": buyer,
        # The same address in parts, for the e-invoice; None if it isn't complete.
        "buyer_address": {
            "name": order.recipient_name, "street1": order.recipient_street1,
            "street2": order.recipient_street2, "zip": order.recipient_zip, "city": order.recipient_city,
        } if buyer else None,
        "payment_terms": payment_terms,
        "order_ref": order.external_order_ref,
        "delivery_date": delivery_date.isoformat(),
        "vat_mode": "regular" if vat_mode == "regular" else "kleinunternehmer",
        "lines": lines,
        "gross": _money(gross),
    }
    if snapshot["vat_mode"] == "regular":
        net, vat = split_vat(gross)
        snapshot.update(net=_money(net), vat=_money(vat), vat_rate=str(VAT_RATE))
    # Fail now, before a number is used up, if some text can't be printed.
    for text in [*buyer, *(line["title"] for line in lines), profile.legal_name, profile.address,
                 profile.email or "", order.external_order_ref or ""]:
        pdf_text(text)
    return snapshot


def storno_snapshot(original: dict, original_number: str, original_date: date) -> dict:
    """The same invoice with all amounts negated, pointing back to the original."""
    def neg(value: str) -> str:
        return _money(-Decimal(value))

    snapshot = dict(original)
    snapshot["lines"] = [{**line, "unit_price": neg(line["unit_price"]), "total": neg(line["total"])}
                         for line in original["lines"]]
    for key in ("gross", "net", "vat"):
        if key in snapshot:
            snapshot[key] = neg(snapshot[key])
    snapshot["cancels"] = {"number": original_number, "date": original_date.isoformat()}
    return snapshot


def _de_date(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d.%m.%Y")


def _eur(value: str) -> str:
    return f"{Decimal(value):.2f}".replace(".", ",") + " €"


def render_pdf(snapshot: dict, number: str, issue_date: date) -> bytes:
    pdf = FPDF(format="A4", unit="mm")
    pdf.core_fonts_encoding = "windows-1252"
    pdf.set_margins(20, 18, 20)
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    def line(text: str, height: float = 5, style: str = "", size: int = 10):
        pdf.set_font("Helvetica", style, size)
        pdf.multi_cell(0, height, pdf_text(text), new_x="LMARGIN", new_y="NEXT")

    seller = snapshot["seller"]
    line(seller["name"], 7, "B", 13)
    for part in seller["address"].splitlines():
        line(part, size=9)
    if seller.get("tax_number"):
        line(f"Steuernummer: {seller['tax_number']}", size=9)
    if seller.get("vat_id"):
        line(f"USt-IdNr.: {seller['vat_id']}", size=9)
    if seller.get("email"):
        line(seller["email"], size=9)

    pdf.ln(10)
    for part in snapshot["buyer"]:
        line(part)

    pdf.ln(10)
    cancels = snapshot.get("cancels")
    line(f"{'Stornorechnung' if cancels else 'Rechnung'} {number}", 9, "B", 16)
    line(f"Rechnungsdatum: {issue_date.strftime('%d.%m.%Y')}", 6)
    line(f"Lieferdatum: {_de_date(snapshot['delivery_date'])}", 6)
    if snapshot.get("order_ref") and not snapshot["order_ref"].startswith("deleted:"):
        line(f"Bestellnummer: {snapshot['order_ref']}", 6)
    if cancels:
        line(f"Storno zur Rechnung {cancels['number']} vom {_de_date(cancels['date'])}", 6)

    pdf.ln(6)
    pdf.set_font("Helvetica", size=10)
    with pdf.table(col_widths=(94, 18, 29, 29), text_align=("LEFT", "RIGHT", "RIGHT", "RIGHT"),
                   borders_layout="HORIZONTAL_LINES", line_height=6) as table:
        head = table.row()
        for title in ("Beschreibung", "Menge", "Einzelpreis", "Betrag"):
            head.cell(title)
        for item in snapshot["lines"]:
            row = table.row()
            for value in (item["title"], str(item["quantity"]), _eur(item["unit_price"]), _eur(item["total"])):
                row.cell(pdf_text(value))

    pdf.ln(5)

    def total(label: str, value: str, style: str = ""):
        pdf.set_font("Helvetica", style, 11 if style else 10)
        pdf.cell(140, 7, pdf_text(label))
        pdf.cell(0, 7, pdf_text(_eur(value)), align="R", new_x="LMARGIN", new_y="NEXT")

    if snapshot["vat_mode"] == "regular":
        total("Nettobetrag", snapshot["net"])
        total(f"enthaltene USt. {snapshot['vat_rate']} %", snapshot["vat"])
        total("Gesamtbetrag", snapshot["gross"], "B")
    else:
        total("Gesamtbetrag", snapshot["gross"], "B")
        pdf.ln(3)
        line("Gemäß § 19 UStG wird keine Umsatzsteuer berechnet.")

    return bytes(pdf.output())
