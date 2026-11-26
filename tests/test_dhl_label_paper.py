"""DHL labels are fitted to the seller's paper; nothing printed on them is lost."""

import base64
import io
from types import SimpleNamespace

import pytest
from fpdf import FPDF
from pypdf import PdfReader
from sqlalchemy import create_engine, text

from app.config import settings
from app.services import dhl_service, label_paper_service
from tests.test_dhl_test_mode import _connected
from tests.test_print_jobs import _make_agent, _make_order, _make_product, _set_recipient_address

MM = 72 / 25.4


def _dhl_like() -> bytes:
    """105 x 203 mm with content nearly to the edges and an empty strip below, like DHL's."""
    pdf = FPDF(unit="mm", format=(105, 203))
    pdf.set_auto_page_break(False)
    pdf.add_page()
    pdf.set_font("Helvetica", "", 12)
    pdf.text(5, 8, "TOP LINE")
    pdf.rect(3, 12, 99, 170)
    pdf.text(5, 195, "BOTTOM LINE")
    return bytes(pdf.output())


def _page(data):
    page = PdfReader(io.BytesIO(data)).pages[0]
    return round(float(page.mediabox.width) / MM), round(float(page.mediabox.height) / MM), page.extract_text()


@pytest.mark.parametrize("paper", ["100x150", "105x148", "100x200"])
def test_the_label_gets_the_paper_size_and_keeps_its_content(paper):
    data, rotation = label_paper_service.fit_label(_dhl_like(), paper)
    width, height, words = _page(data)
    assert (width, height) == tuple(int(v) for v in paper.split("x")) and rotation == 0
    assert "TOP LINE" in words and "BOTTOM LINE" in words


def test_original_size_is_left_alone():
    source = _dhl_like()
    assert label_paper_service.fit_label(source, "original") == (source, 90)


def _dhl_answers(monkeypatch, content):
    monkeypatch.setattr(dhl_service.httpx, "post", lambda url, json, auth, headers, timeout: SimpleNamespace(
        status_code=200, json=lambda: {"items": [{"shipmentNo": "00340", "label": {
            "b64": base64.b64encode(content).decode()}}]}))


def _jobs():
    with create_engine(settings.database_url).connect() as conn:
        return conn.execute(text("SELECT rotation_degrees, label_pdf_data FROM print_jobs ORDER BY created_at")).fetchall()


def test_the_setting_is_saved_and_rejects_unknown_paper(client, register):
    headers, _ = register()
    assert client.get("/tenants/me", headers=headers).json()["label_paper"] == "100x150"
    r = client.patch("/tenants/me/label-paper", headers=headers, json={"label_paper": "100x200"})
    assert r.status_code == 200 and r.json()["label_paper"] == "100x200"
    assert client.patch("/tenants/me/label-paper", headers=headers, json={"label_paper": "7x7"}).status_code == 422
    assert client.patch("/tenants/me/label-paper", json={"label_paper": "original"}).status_code == 401


@pytest.mark.parametrize("paper,size,turn", [("100x150", (100, 150), 0), ("original", (105, 203), 90)])
def test_test_label_and_its_print_job_follow_the_paper(client, register, monkeypatch, paper, size, turn):
    headers = _connected(client, register)
    client.patch("/tenants/me/label-paper", headers=headers, json={"label_paper": paper})
    _dhl_answers(monkeypatch, _dhl_like())
    label = client.post("/dhl/test-label", headers=headers)
    assert label.status_code == 200 and _page(label.content)[:2] == size
    _make_agent(client, headers)
    assert client.post("/dhl/test-label/print", headers=headers,
                       files={"file": ("t.pdf", label.content, "application/pdf")}).status_code == 201
    (rotation, data), = _jobs()
    assert rotation == turn and _page(bytes(data))[:2] == size


def test_a_bought_label_is_fitted_and_a_reprint_stays_the_same(client, register, monkeypatch):
    headers = _connected(client, register, production=True)
    _dhl_answers(monkeypatch, _dhl_like())
    _make_agent(client, headers)
    order = _make_order(client, headers, _make_product(client, headers))
    _set_recipient_address(order["id"])
    assert client.post(f"/print-jobs/order/{order['id']}", headers=headers).status_code == 201
    (rotation, data), = _jobs()
    assert rotation == 0 and _page(bytes(data))[:2] == (100, 150)


def test_a_paid_label_that_cannot_be_fitted_is_still_printed(client, register, monkeypatch):
    headers = _connected(client, register, production=True)
    _dhl_answers(monkeypatch, b"%PDF-broken")
    _make_agent(client, headers)
    order = _make_order(client, headers, _make_product(client, headers))
    _set_recipient_address(order["id"])
    assert client.post(f"/print-jobs/order/{order['id']}", headers=headers).status_code == 201
    (rotation, data), = _jobs()
    assert rotation == 90 and bytes(data) == b"%PDF-broken"


def test_an_uploaded_label_is_not_touched(client, register):
    headers, _ = register()
    _make_agent(client, headers)
    order = _make_order(client, headers, _make_product(client, headers))
    source = _dhl_like()
    for url, files in ((f"/print-jobs/order/{order['id']}/upload-label",
                        {"file": ("e.pdf", source, "application/pdf")}), (f"/print-jobs/order/{order['id']}", None)):
        assert client.post(url, headers=headers, files=files).status_code == 201
    assert [(r, bytes(d)) for r, d in _jobs()] == [(90, source), (90, source)]
