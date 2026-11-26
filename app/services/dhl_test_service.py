"""A DHL test label that has nothing to do with any order.

The consignee is made up, nothing is stored, and the page is stamped so that a
printed test label cannot be mistaken for postage.
"""

import io
import uuid
from types import SimpleNamespace

from fpdf import FPDF
from pypdf import PdfReader, PdfWriter

STAMP = "TESTETIKETT - NICHT VERSENDEN"


def sample_order() -> SimpleNamespace:
    """Looks like an order to the label service; it is none and exists nowhere."""
    return SimpleNamespace(
        id=uuid.uuid4(), recipient_name="Max Mustermann", recipient_street1="Kurt-Schumacher-Strasse 20",
        recipient_street2=None, recipient_zip="53113", recipient_city="Bonn", recipient_state=None,
        recipient_country_code="DE", recipient_phone=None)


def stamp(pdf: bytes) -> bytes:
    """Writes the warning across every page. Raises if the file cannot be read."""
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(pdf)))
    for page in writer.pages:
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        overlay = FPDF(unit="pt", format=(width, height))
        overlay.set_auto_page_break(False)
        overlay.set_margins(0, 0, 0)
        overlay.add_page()
        size = min(width, height) / 16
        overlay.set_font("Helvetica", "B", size)
        overlay.set_text_color(220, 0, 0)
        text_width = overlay.get_string_width(STAMP)
        with overlay.rotation(35, x=width / 2, y=height / 2):
            for offset in (-height / 4, 0, height / 4):
                overlay.text((width - text_width) / 2, height / 2 + offset, STAMP)
        page.merge_page(PdfReader(io.BytesIO(bytes(overlay.output()))).pages[0])
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()
