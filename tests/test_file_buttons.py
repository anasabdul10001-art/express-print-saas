"""Visible file fields get their button text from the page, not from the browser."""
import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


def test_every_page_with_a_visible_file_field_loads_the_script():
    checked = []
    for page in FRONTEND.rglob("*.html"):
        html = page.read_text(encoding="utf-8")
        fields = re.findall(r"<input[^>]*type=\"file\"[^>]*>", html, re.S)
        if any(not re.search(r'class="(?:[^"]*\s)?hidden(?:\s[^"]*)?"', field) for field in fields):
            assert "file-input.js" in html, page.name
            checked.append(page.name)
    assert {"expenses.html", "label-print.html", "purchases.html"} <= set(checked)
