"""The print agent's label fitting, on a PDF laid out like eBay's: a small label with crop marks on A4."""

import importlib.util
from pathlib import Path

import pypdfium2
from fpdf import FPDF
from PIL import Image, ImageDraw
from pypdf import PdfReader

_spec = importlib.util.spec_from_file_location(
    "label_fit", Path(__file__).parent.parent / "print-agent" / "label_fit.py"
)
label_fit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(label_fit)


def _ink_box_mm(path, page_index=0):
    page = pypdfium2.PdfDocument(str(path))[page_index]
    gray = page.render(scale=3).to_pil().convert("L")
    box = gray.point(lambda value: 255 if value < 200 else 0).getbbox()
    return [value / 3 / label_fit.MM_TO_PT for value in box], gray


def _write_ebay_style_label(path):
    pdf = FPDF(format="A4", unit="mm")
    pdf.add_page()
    # Label content: 80 x 30 mm starting at (20, 40).
    pdf.set_fill_color(0, 0, 0)
    pdf.rect(20, 40, 80, 30, style="F")
    # Crop marks, centred on the corners of a box around it.
    for x, y in [(15, 35), (105, 35), (15, 75), (105, 75)]:
        pdf.line(x - 2.5, y, x + 2.5, y)
        pdf.line(x, y - 2.5, x, y + 2.5)
    pdf.output(str(path))


def test_fits_label_to_paper_rotated_and_without_crop_marks(tmp_path):
    source, output = tmp_path / "in.pdf", tmp_path / "out.pdf"
    _write_ebay_style_label(source)

    label_fit.fit_label_to_paper(str(source), str(output), (100, 150))

    page = PdfReader(str(output)).pages[0]
    assert round(float(page.mediabox.width) / label_fit.MM_TO_PT) == 100
    assert round(float(page.mediabox.height) / label_fit.MM_TO_PT) == 150

    (left, top, right, bottom), gray = _ink_box_mm(output)
    # Turned to stand upright, the 80 mm side fills the 146 mm usable height.
    assert bottom - top > 140
    assert 50 < right - left < 60
    # The content is one solid rectangle: stray crop marks would leave white gaps.
    inner = gray.crop(gray.point(lambda value: 255 if value < 200 else 0).getbbox())
    assert sum(inner.histogram()[200:]) / (inner.width * inner.height) < 0.01


def test_blank_pdf_is_rejected(tmp_path):
    source = tmp_path / "blank.pdf"
    pdf = FPDF(format="A4")
    pdf.add_page()
    pdf.output(str(source))

    try:
        label_fit.fit_label_to_paper(str(source), str(tmp_path / "out.pdf"))
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_label_reaching_its_corners_is_not_cut():
    ink = Image.new("L", (300, 200), 0)
    draw = ImageDraw.Draw(ink)
    draw.rectangle((10, 10, 209, 109), fill=255)
    assert label_fit.find_content_box(ink, 20) == (10, 10, 210, 110)
