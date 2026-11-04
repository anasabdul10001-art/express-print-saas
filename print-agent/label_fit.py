"""
Fits a label PDF to the printer's paper: finds where the label actually is on
the page (eBay puts a small label on an A4 sheet), drops the crop marks around
it, then rotates and enlarges it to fill the paper. Same behaviour as the
website's label print page (frontend/app/label-tools.js).

pypdfium2 renders the page so we can see the ink; pypdf rebuilds the page,
which keeps text and barcodes as sharp vector content instead of an image.
"""

import pypdfium2
from PIL import Image
from pypdf import PageObject, PdfReader, PdfWriter, Transformation
from pypdf.generic import RectangleObject

MM_TO_PT = 72 / 25.4
RENDER_SCALE = 3
INK_THRESHOLD = 200
MARGIN_MM = 2
PADDING_PT = 1
# eBay's corner crop marks reach about 5 mm in from the content's edge, and the
# label's own text starts just past that.
CROP_MARK_BAND_MM = 5.5

DEFAULT_PAPER_MM = (100, 150)


def find_content_box(ink: Image.Image, band: int) -> tuple[int, int, int, int] | None:
    """
    (left, top, right, bottom) of the ink in a mode "L" image where ink is
    non-zero. Crop marks are left out only when all four corners hold ink and
    the edges between them are empty, so a label that really does reach its
    corners is never cut.
    """
    box = ink.getbbox()
    if not box:
        return None

    left, top, right, bottom = box
    if right - left <= 2 * band or bottom - top <= 2 * band:
        return box

    def has_ink(x0: int, y0: int, x1: int, y1: int) -> bool:
        return ink.crop((x0, y0, x1, y1)).getbbox() is not None

    corners_inked = (
        has_ink(left, top, left + band, top + band)
        and has_ink(right - band, top, right, top + band)
        and has_ink(left, bottom - band, left + band, bottom)
        and has_ink(right - band, bottom - band, right, bottom)
    )
    edges_empty = not (
        has_ink(left + band, top, right - band, top + band)
        or has_ink(left + band, bottom - band, right - band, bottom)
        or has_ink(left, top + band, left + band, bottom - band)
        or has_ink(right - band, top + band, right, bottom - band)
    )
    if not (corners_inked and edges_empty):
        return box

    inner = ink.crop((left + band, top + band, right - band, bottom - band)).getbbox()
    if not inner:
        return box
    return (left + band + inner[0], top + band + inner[1], left + band + inner[2], top + band + inner[3])


def compute_placement(
    label_w: float, label_h: float, paper_w: float, paper_h: float, margin: float
) -> tuple[bool, float, float, float]:
    """(rotate, scale, x, y): the largest fit, turned 90 degrees if that's bigger."""
    avail_w = paper_w - 2 * margin
    avail_h = paper_h - 2 * margin
    straight = min(avail_w / label_w, avail_h / label_h)
    turned = min(avail_w / label_h, avail_h / label_w)
    rotate = turned > straight
    scale = turned if rotate else straight
    drawn_w = (label_h if rotate else label_w) * scale
    drawn_h = (label_w if rotate else label_h) * scale
    left = (paper_w - drawn_w) / 2
    bottom = (paper_h - drawn_h) / 2
    # A 90-degree turn pivots around the label's bottom-left corner, which then
    # sits at the drawn area's bottom-right.
    return rotate, scale, left + drawn_w if rotate else left, bottom


def _page_content_box(pdfium_page) -> tuple[float, float, float, float] | None:
    """(left, bottom, right, top) in PDF points, or None for a blank page."""
    # rotation=0 draws the page's raw content, matching the coordinates pypdf
    # transforms. The default would apply the page's /Rotate first.
    gray = pdfium_page.render(scale=RENDER_SCALE, rotation=0, may_draw_forms=False).to_pil().convert("L")
    ink = gray.point(lambda value: 255 if value < INK_THRESHOLD else 0)
    box = find_content_box(ink, round(CROP_MARK_BAND_MM * MM_TO_PT * RENDER_SCALE))
    if not box:
        return None

    page_left, _, _, page_top = pdfium_page.get_bbox()
    return (
        page_left + box[0] / RENDER_SCALE - PADDING_PT,
        page_top - box[3] / RENDER_SCALE - PADDING_PT,
        page_left + box[2] / RENDER_SCALE + PADDING_PT,
        page_top - box[1] / RENDER_SCALE + PADDING_PT,
    )


def fit_label_to_paper(input_path: str, output_path: str, paper_mm: tuple[float, float] = DEFAULT_PAPER_MM) -> None:
    """Raises ValueError if the PDF has nothing on it to print."""
    paper_w = paper_mm[0] * MM_TO_PT
    paper_h = paper_mm[1] * MM_TO_PT
    margin = MARGIN_MM * MM_TO_PT

    reader = PdfReader(input_path)
    writer = PdfWriter()
    rendered = pypdfium2.PdfDocument(input_path)
    try:
        for index, source in enumerate(reader.pages):
            box = _page_content_box(rendered[index])
            if not box:
                continue

            left, bottom, right, top = box
            rotate, scale, x, y = compute_placement(right - left, top - bottom, paper_w, paper_h, margin)

            rectangle = RectangleObject((left, bottom, right, top))
            source.mediabox = rectangle
            source.cropbox = rectangle
            source.trimbox = rectangle

            transform = Transformation().translate(-left, -bottom).scale(scale)
            if rotate:
                transform = transform.rotate(90)
            transform = transform.translate(x, y)

            target = PageObject.create_blank_page(width=paper_w, height=paper_h)
            target.merge_transformed_page(source, transform)
            writer.add_page(target)
    finally:
        rendered.close()

    if len(writer.pages) == 0:
        raise ValueError("Das PDF enthält nichts zum Drucken.")

    with open(output_path, "wb") as f:
        writer.write(f)
