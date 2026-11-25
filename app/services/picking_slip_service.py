"""
The "integrated" print mode: the shipping label followed by a 4x6 picking slip
(product picture, name, quantity, shelf location) in one PDF. The label pages are
passed through unchanged.
"""

import io
import unicodedata
from urllib.parse import urlparse

import httpx
from fpdf import FPDF
from pypdf import PdfReader, PdfWriter

SLIP_FORMAT = (101.6, 152.4)  # 4 x 6 inches in mm
# The server only fetches product pictures from the marketplaces' own image hosts
# (an image URL is seller input, so anything else could point into our own network).
IMAGE_HOSTS = ("i.ebayimg.com", "cdn.shopify.com", "m.media-amazon.com", "images-eu.ssl-images-amazon.com")
MAX_IMAGE_BYTES = 3 * 1024 * 1024
MAX_PICTURES = 8


def _text(value: str) -> str:
    """The built-in font only knows Windows-1252; a picking slip may simply show '?' otherwise."""
    return unicodedata.normalize("NFC", value or "").encode("cp1252", "replace").decode("cp1252")


def fetch_picture(url: str | None) -> bytes | None:
    parsed = urlparse(url or "")
    if parsed.scheme != "https" or parsed.hostname not in IMAGE_HOSTS:
        return None
    try:
        response = httpx.get(url, timeout=6.0, follow_redirects=False)
    except httpx.HTTPError:
        return None
    if response.status_code != 200 or len(response.content) > MAX_IMAGE_BYTES:
        return None
    return response.content


def picking_slip(order) -> bytes:
    pdf = FPDF(format=SLIP_FORMAT, unit="mm")
    pdf.core_fonts_encoding = "windows-1252"
    pdf.set_margins(5, 5, 5)
    pdf.set_auto_page_break(auto=True, margin=5)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 13)
    pdf.multi_cell(0, 6, _text(f"Packzettel {order.external_order_ref or ''}"), new_x="LMARGIN", new_y="NEXT")
    if order.recipient_name:
        pdf.set_font("Helvetica", size=9)
        pdf.multi_cell(0, 5, _text(order.recipient_name), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    for position, item in enumerate(order.items):
        product = item.product
        shelf = (product.shelf_location if product else None) or item.shelf_location or "-"
        if pdf.get_y() > SLIP_FORMAT[1] - 35:
            pdf.add_page()
        top = pdf.get_y()
        picture = fetch_picture(product.image_url if product else None) if position < MAX_PICTURES else None
        if picture:
            try:
                pdf.image(io.BytesIO(picture), x=5, y=top, w=25, h=25, keep_aspect_ratio=True)
            except Exception:  # noqa: BLE001 - a broken picture must never block a shipment
                pass
        pdf.set_xy(33, top)
        pdf.set_font("Helvetica", "B", 12)
        pdf.multi_cell(63, 5.5, _text(f"{item.quantity} x    Regal: {shelf}"), new_x="LEFT", new_y="NEXT")
        pdf.set_font("Helvetica", size=9)
        title = product.title if product else "Produkt nicht mehr vorhanden"
        pdf.multi_cell(63, 4, _text(title[:140]), new_x="LEFT", new_y="NEXT")
        bottom = max(pdf.get_y(), top + 26)
        pdf.line(5, bottom + 1, SLIP_FORMAT[0] - 5, bottom + 1)
        pdf.set_xy(5, bottom + 3)
    return bytes(pdf.output())


def combine(label_pdf: bytes, order) -> bytes:
    """Label pages first, then the picking slip."""
    writer = PdfWriter()
    for source in (label_pdf, picking_slip(order)):
        for page in PdfReader(io.BytesIO(source)).pages:
            writer.add_page(page)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()
