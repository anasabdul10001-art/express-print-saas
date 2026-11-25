"""Reading supplier invoices with AI: the provider chain and the checks on its answers."""
import io
import json
from decimal import Decimal

import httpx
import pytest
from fpdf import FPDF
from PIL import Image

from app.core.rate_limit import limiter
from app.services import invoice_ai_service as ai


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    limiter.reset()
    for name, value in (("groq_api_key", "g"), ("mistral_api_key", "m"), ("openrouter_api_key", "o")):
        monkeypatch.setattr(ai.settings, name, value)


def _answer(**changes):
    data = {"supplier": "Shenzhen Cable Co", "invoice_number": "INV-7", "invoice_date": "2026-11-20",
            "currency": "EUR", "lines": [{"description": "USB-C cable", "quantity": 30, "line_total": 18.0,
                                          "product_number": 1}],
            "extra_costs": 2, "discount": 0, "tax_amount": 0, "total": 20.0}
    data.update(changes)
    return json.dumps(data)


def _fake_post(monkeypatch, replies):
    """replies: per host, a status code or the model's answer text. Records the requests."""
    calls = []

    def post(url, headers=None, timeout=None, json=None):
        host = httpx.URL(url).host
        calls.append((host, json))
        reply = replies[host]
        request = httpx.Request("POST", url)
        if isinstance(reply, int):
            return httpx.Response(reply, request=request)
        return httpx.Response(200, request=request, json={"choices": [{"message": {"content": reply}}]})

    monkeypatch.setattr(ai.httpx, "post", post)
    return calls


GROQ, MISTRAL, ROUTER = "api.groq.com", "api.mistral.ai", "openrouter.ai"


def _text_pdf(extra=""):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    pdf.multi_cell(0, 6, "Invoice INV-7 from Shenzhen Cable Co dated 2026-11-20. 30 pcs USB-C cable 18.00 EUR. "
                         "Shipping 2.00 EUR. Total 20.00 EUR. " + extra)
    return bytes(pdf.output())


def _png():
    buffer = io.BytesIO()
    Image.new("RGB", (3000, 1000), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def test_a_used_up_quota_falls_through_to_the_next_provider(monkeypatch):
    calls = _fake_post(monkeypatch, {GROQ: 429, MISTRAL: _answer()})
    result = ai.read_invoice(_text_pdf(), "application/pdf", ["USB-C Kabel"])
    assert [c[0] for c in calls] == [GROQ, MISTRAL]
    assert result["provider"] == "Mistral" and result["matches"]
    assert result["failures"] == ["Groq: Kontingent aufgebraucht"]
    assert result["lines"][0]["line_total"] == Decimal("18.00")


def test_an_answer_that_does_not_add_up_lets_the_next_provider_try(monkeypatch):
    calls = _fake_post(monkeypatch, {GROQ: _answer(total=25), MISTRAL: _answer()})
    assert ai.read_invoice(_text_pdf(), "application/pdf", ["x"])["provider"] == "Mistral"
    assert len(calls) == 2


def test_if_none_adds_up_the_first_answer_comes_with_a_warning(monkeypatch):
    _fake_post(monkeypatch, {GROQ: _answer(total=25), MISTRAL: "no idea", ROUTER: 500})
    result = ai.read_invoice(_text_pdf(), "application/pdf", ["x"])
    assert result["provider"] == "Groq" and not result["matches"]
    assert any(w.startswith("Achtung") for w in result["warnings"])


def test_all_failing_gives_one_readable_message(monkeypatch):
    _fake_post(monkeypatch, {GROQ: 429, MISTRAL: 429, ROUTER: 429})
    with pytest.raises(ai.InvoiceReadError, match="Kontingent"):
        ai.read_invoice(_text_pdf(), "application/pdf", [])


def test_a_text_pdf_is_sent_as_text_without_mail_and_iban(monkeypatch):
    calls = _fake_post(monkeypatch, {GROQ: _answer()})
    ai.read_invoice(_text_pdf("Contact sales@example.com IBAN DE89 3704 0044 0532 0130 00"),
                    "application/pdf", ["USB-C Kabel"])
    sent = calls[0][1]["messages"][1]["content"]
    assert isinstance(sent, str) and "1. USB-C Kabel" in sent and "USB-C cable" in sent
    assert "sales@example.com" not in sent and "0532" not in sent


def test_a_photo_is_sent_as_a_smaller_picture(monkeypatch):
    calls = _fake_post(monkeypatch, {GROQ: _answer()})
    ai.read_invoice(_png(), "image/png", [])
    parts = calls[0][1]["messages"][1]["content"]
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_a_scanned_pdf_with_many_pages_skips_the_provider_with_a_page_limit(monkeypatch):
    pdf = FPDF()
    for _ in range(4):
        pdf.add_page()
        pdf.image(io.BytesIO(_png()), w=100)
    calls = _fake_post(monkeypatch, {MISTRAL: _answer()})
    result = ai.read_invoice(bytes(pdf.output()), "application/pdf", [])
    assert [c[0] for c in calls] == [MISTRAL]
    assert len(calls[0][1]["messages"][1]["content"]) == 5 and result["failures"] == ["Groq: zu viele Seiten"]


def test_openrouter_only_uses_hosts_that_do_not_keep_prompts(monkeypatch):
    calls = _fake_post(monkeypatch, {GROQ: 429, MISTRAL: 429, ROUTER: _answer()})
    ai.read_invoice(_text_pdf(), "application/pdf", [])
    assert calls[-1][1]["provider"] == {"data_collection": "deny"}


def test_damaged_files_are_refused_before_anything_is_sent(monkeypatch):
    calls = _fake_post(monkeypatch, {})
    for content, kind in ((b"%PDF-1.4 broken", "application/pdf"), (b"\x89PNG\r\n\x1a\nbroken", "image/png")):
        with pytest.raises(ai.InvoiceReadError):
            ai.read_invoice(content, kind, [])
    assert calls == []


def test_discount_is_spread_over_the_lines_to_the_cent():
    lines = [{"description": d, "quantity": 1, "line_total": 10, "product_number": None} for d in "abc"]
    result = ai.parse(_answer(lines=lines, extra_costs=0, discount=1, total=29), 0)
    assert sum(l["line_total"] for l in result["lines"]) == Decimal("29.00") and result["matches"]


def test_net_lines_with_vat_on_top_are_recognised():
    result = ai.parse(_answer(tax_amount=3.8, total=23.8), 1)
    assert result["net"] and not result["matches"]


@pytest.mark.parametrize("bad", [
    {"lines": []},
    {"lines": [{"description": "a", "quantity": 0, "line_total": 1}]},
    {"lines": [{"description": "a", "quantity": 1.5, "line_total": 1}]},
    {"lines": [{"description": "a", "quantity": 1, "line_total": -1}]},
    {"lines": [{"description": "a", "quantity": 1, "line_total": "NaN"}]},
    {"lines": ["a"]},
])
def test_unusable_lines_are_rejected(bad):
    with pytest.raises(ValueError):
        ai.parse(_answer(**bad), 1)


def test_unknown_product_numbers_and_bad_dates_are_dropped():
    lines = [{"description": "a", "quantity": 1, "line_total": 1, "product_number": 9}]
    result = ai.parse("```json\n" + _answer(lines=lines, invoice_date="20.11.2026", total=3) + "\n```", 2)
    assert result["lines"][0]["product_number"] is None and result["invoice_date"] is None


def test_the_endpoint_suggests_own_products_and_changes_nothing(client, register, monkeypatch):
    _fake_post(monkeypatch, {GROQ: _answer()})
    other, _ = register()
    client.post("/products", headers=other, json={"sku": "X", "title": "AAA fremd", "selling_price": 5, "stock_quantity": 1})
    headers, _ = register()
    pid = client.post("/products", headers=headers, json={"sku": "A", "title": "USB-C Kabel", "selling_price": 5,
                                                          "stock_quantity": 10, "purchase_price": 1}).json()["id"]
    r = client.post("/purchases/read", headers=headers, files={"file": ("r.pdf", _text_pdf(), "application/pdf")})
    assert r.status_code == 200, r.text
    assert r.json()["lines"][0]["product_id"] == pid and r.json()["total"] == "20.00"
    product = client.get("/products", headers=headers).json()[0]
    assert product["stock_quantity"] == 10 and Decimal(str(product["purchase_price"])) == 1
    assert client.get("/purchases", headers=headers).json() == []
    assert client.get("/purchases/ai-status", headers=headers).json() == {"providers": ["Groq", "Mistral", "OpenRouter"]}


def test_the_endpoint_needs_login_and_a_supported_file(client, register):
    assert client.post("/purchases/read", files={"file": ("r.pdf", b"%PDF", "application/pdf")}).status_code in (401, 403, 422)
    headers, _ = register()
    r = client.post("/purchases/read", headers=headers, files={"file": ("r.txt", b"hello", "text/plain")})
    assert r.status_code == 400
