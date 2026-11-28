"""
Builds the subscription invoice PDF (German with English labels) from an
Invoice row and the platform's SiteSettings (seller details). As a small
business it follows § 34a UStDV, otherwise § 14 (4) UStG.

The PDF is built once, when the invoice is issued, and stored with the row
(Invoice.pdf): a later change of the seller's details must not change an
issued invoice. Pure Python (fpdf2, no system dependencies).
"""

from fpdf import FPDF

from app.models.invoice import Invoice
from app.models.plan import SiteSettings
from app.services.sales_invoice_service import pdf_text

PAYMENT_LABELS = {
    "stripe": "Stripe (Online-Zahlung / online payment)",
    "manual": "Zahlung erhalten / payment received",
}
COUNTRY_NAMES = {"DE": "Deutschland / Germany"}


def _line(pdf: FPDF, text: str, height: float = 5) -> None:
    pdf.multi_cell(0, height, pdf_text(text), new_x="LMARGIN", new_y="NEXT")


def _pair(pdf: FPDF, label: str, value: str) -> None:
    pdf.cell(72, 6, pdf_text(label))
    pdf.multi_cell(0, 6, pdf_text(value), new_x="LMARGIN", new_y="NEXT")


def generate_invoice_pdf(invoice: Invoice, settings_row: SiteSettings, cancels_number: str | None = None,
                         cancels_date=None) -> bytes:
    pdf = FPDF(format="A4", unit="mm")
    pdf.core_fonts_encoding = "cp1252"
    pdf.set_margins(20, 18, 20)
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.set_title(f"Rechnung {invoice.invoice_number}")
    pdf.add_page()

    # --- Seller ---
    pdf.set_font("Helvetica", "B", 13)
    _line(pdf, settings_row.company_legal_name or "", 7)
    pdf.set_font("Helvetica", size=9)
    for line in (settings_row.company_address or "").splitlines():
        _line(pdf, line)
    if settings_row.company_tax_id:
        _line(pdf, f"Steuernummer / USt-IdNr. (Tax ID): {settings_row.company_tax_id}")
    if settings_row.company_email:
        _line(pdf, settings_row.company_email)
    pdf.ln(9)

    # --- Customer ---
    pdf.set_font("Helvetica", size=8)
    _line(pdf, "Rechnungsempfänger / Bill to", 4)
    pdf.set_font("Helvetica", size=10)
    country = COUNTRY_NAMES.get(invoice.customer_country or "", invoice.customer_country)
    for line in (invoice.customer_name, invoice.customer_street, invoice.customer_zip_city, country):
        if line:
            _line(pdf, line)
    if invoice.customer_vat_id:
        _line(pdf, f"USt-IdNr. (VAT ID): {invoice.customer_vat_id}")
    _line(pdf, invoice.customer_email)
    pdf.ln(9)

    # --- Header ---
    storno = invoice.cancels_id is not None
    pdf.set_font("Helvetica", "B", 15)
    title = "Stornorechnung / Cancellation invoice" if storno else "Rechnung / Invoice"
    _line(pdf, f"{title} {invoice.invoice_number}", 9)
    pdf.set_font("Helvetica", size=10)
    _pair(pdf, "Rechnungsdatum / Invoice date:", invoice.issue_date.strftime("%d.%m.%Y"))
    if invoice.service_start and invoice.service_end:
        _pair(pdf, "Leistungszeitraum / Service period:",
              f"{invoice.service_start:%d.%m.%Y} - {invoice.service_end:%d.%m.%Y}")
    else:
        _pair(pdf, "Leistungsdatum / Service date:", invoice.issue_date.strftime("%d.%m.%Y"))
    if invoice.paid_on:
        _pair(pdf, "Bezahlt am / Paid on:", invoice.paid_on.strftime("%d.%m.%Y"))
    if invoice.payment_method:
        _pair(pdf, "Zahlungsart / Payment:", PAYMENT_LABELS.get(invoice.payment_method, invoice.payment_method))
    if storno and cancels_number:
        of = f" vom {cancels_date:%d.%m.%Y}" if cancels_date else ""
        _line(pdf, f"Storno zur Rechnung {cancels_number}{of} / Cancels invoice {cancels_number}.", 6)
    pdf.ln(7)

    # --- Line item ---
    without_vat = invoice.vat_rate == 0
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_draw_color(200, 200, 200)
    pdf.cell(135, 8, "Leistung / Service", border="B")
    pdf.cell(0, 8, "Betrag / Amount" if without_vat else "Netto / Net", border="B", align="R",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=10)
    pdf.ln(1)
    top = pdf.get_y()
    pdf.multi_cell(130, 6, pdf_text(invoice.description), align="L", new_x="LMARGIN", new_y="NEXT")
    bottom = pdf.get_y()
    pdf.set_xy(pdf.l_margin + 135, top)
    pdf.cell(0, 6, f"{invoice.net_amount:.2f} {invoice.currency}", align="R")
    pdf.set_xy(pdf.l_margin, bottom + 5)

    if not without_vat:
        pdf.cell(135, 6, "Nettobetrag / Net total")
        pdf.cell(0, 6, f"{invoice.net_amount:.2f} {invoice.currency}", align="R", new_x="LMARGIN", new_y="NEXT")
        pdf.cell(135, 6, f"zzgl. {invoice.vat_rate:.0f}% USt. / VAT")
        pdf.cell(0, 6, f"{invoice.vat_amount:.2f} {invoice.currency}", align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(135, 9, "Gesamtbetrag / Total")
    pdf.cell(0, 9, f"{invoice.gross_amount:.2f} {invoice.currency}", align="R", new_x="LMARGIN", new_y="NEXT")

    if without_vat:
        pdf.ln(3)
        pdf.set_font("Helvetica", size=10)
        _line(pdf, "Steuerbefreiung für Kleinunternehmer nach § 19 UStG: Es wird keine Umsatzsteuer berechnet.")
        _line(pdf, "VAT exempt under the German small business scheme (§ 19 UStG): no VAT is charged.")

    pdf.ln(12)
    pdf.set_font("Helvetica", size=8)
    pdf.set_text_color(120, 120, 120)
    _line(pdf, "Diese Rechnung wurde maschinell erstellt und ist ohne Unterschrift gültig. "
               "Computer-generated invoice, valid without signature.", 4)

    return bytes(pdf.output())
