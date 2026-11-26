"""Every page with marked lists loads the numbering style; the main lists are marked."""
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "frontend" / "app"
MAIN_LISTS = {"products": 'table data-numbered="table"', "expenses": 'table data-numbered="table"',
              "orders": 'id="orders-list" data-numbered="list"', "purchases": 'id="lines" data-numbered="list"',
              "print-agents": 'table data-numbered="table"', "accounting": 'table data-numbered="table"'}


def test_marked_pages_load_the_script_and_main_lists_are_numbered():
    for page in APP.glob("*.html"):
        html = page.read_text(encoding="utf-8")
        if "data-numbered=" in html:
            assert "../list-numbers.js" in html, page.name
    for name, mark in MAIN_LISTS.items():
        assert mark in (APP / f"{name}.html").read_text(encoding="utf-8"), name
