"""
Builds a German §14-UStG-compliant invoice PDF from an Invoice row and the
platform's SiteSettings (seller details). Pure-Python (fpdf2, no system
dependencies like wkhtmltopdf/weasyprint), so it works unmodified on Render
or any other plain Python host.
"""

from fpdf import FPDF

from app.models.invoice import Invoice
from app.models.plan import SiteSettings


def generate_invoice_pdf(invoice: Invoice, settings_row: SiteSettings) -> bytes:
    pdf = FPDF(format="A4", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    # --- Seller block ---
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 7, settings_row.company_legal_name or "", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=9)
    for line in (settings_row.company_address or "").splitlines():
        pdf.cell(0, 5, line, new_x="LMARGIN", new_y="NEXT")
    if settings_row.company_tax_id:
        pdf.cell(0, 5, f"USt-IdNr. / Steuernummer: {settings_row.company_tax_id}", new_x="LMARGIN", new_y="NEXT")
    if settings_row.company_email:
        pdf.cell(0, 5, settings_row.company_email, new_x="LMARGIN", new_y="NEXT")

    pdf.ln(10)

    # --- Customer block ---
    pdf.set_font("Helvetica", size=10)
    pdf.cell(0, 5, invoice.customer_name, new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 5, invoice.customer_email, new_x="LMARGIN", new_y="NEXT")

    pdf.ln(10)

    # --- Invoice header ---
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 9, f"Rechnung {invoice.invoice_number}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", size=10)
    pdf.cell(0, 6, f"Rechnungsdatum: {invoice.issue_date.strftime('%d.%m.%Y')}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, f"Leistungsdatum: {invoice.issue_date.strftime('%d.%m.%Y')}", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(8)

    # --- Line item table ---
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_draw_color(200, 200, 200)
    pdf.cell(130, 8, "Beschreibung", border="B")
    pdf.cell(0, 8, "Netto", border="B", align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", size=10)
    pdf.cell(130, 8, invoice.description)
    pdf.cell(0, 8, f"{invoice.net_amount:.2f} {invoice.currency}", align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(6)

    pdf.cell(150, 6, "Nettobetrag")
    pdf.cell(0, 6, f"{invoice.net_amount:.2f} {invoice.currency}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(150, 6, f"zzgl. {invoice.vat_rate:.0f}% USt.")
    pdf.cell(0, 6, f"{invoice.vat_amount:.2f} {invoice.currency}", align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(150, 9, "Gesamtbetrag")
    pdf.cell(0, 9, f"{invoice.gross_amount:.2f} {invoice.currency}", align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(12)
    pdf.set_font("Helvetica", size=8)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(0, 4, "Diese Rechnung wurde maschinell erstellt und ist ohne Unterschrift gültig.")

    output = pdf.output()
    return bytes(output)
