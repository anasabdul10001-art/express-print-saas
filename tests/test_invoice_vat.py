"""Subscription invoices of a small business must not show VAT."""
import io
from datetime import date
from decimal import Decimal

import pytest
from pypdf import PdfReader

from app.config import settings
from app.models.invoice import Invoice
from app.models.plan import SiteSettings
from app.services import billing_service
from app.services.invoice_service import generate_invoice_pdf


def _text(rate: str, gross: str) -> str:
    invoice = Invoice(invoice_number="2026-0001", issue_date=date(2026, 11, 25), description="Plan PRO",
                      net_amount=Decimal("50.00"), vat_rate=Decimal(rate),
                      vat_amount=Decimal(gross) - Decimal("50.00"), gross_amount=Decimal(gross), currency="EUR",
                      customer_name="Kunde", customer_email="k@example.com")
    seller = SiteSettings(company_legal_name="Anbieter", company_address="Weg 1", company_tax_id="123/456")
    return " ".join(PdfReader(io.BytesIO(generate_invoice_pdf(invoice, seller))).pages[0].extract_text().split())


def test_without_any_setting_no_vat_is_charged():
    assert type(settings).model_fields["operator_small_business"].default is True


def test_the_rate_follows_the_small_business_setting(monkeypatch):
    monkeypatch.setattr(settings, "operator_small_business", True)
    assert billing_service.invoice_vat_rate() == 0
    monkeypatch.setattr(settings, "operator_small_business", False)
    assert billing_service.invoice_vat_rate() == Decimal("19.00")


def test_small_business_invoice_shows_the_exemption_and_no_vat():
    text = _text("0.00", "50.00")
    assert "§ 19 UStG" in text and "keine Umsatzsteuer" in text
    for word in ("zzgl.", "% USt", "Nettobetrag", "Netto "):
        assert word not in text, word
    assert "Gesamtbetrag / Total 50.00 EUR" in text


def test_vat_liable_invoice_is_unchanged():
    text = _text("19.00", "59.50")
    assert "zzgl. 19% USt." in text and "Nettobetrag" in text and "§ 19" not in text
