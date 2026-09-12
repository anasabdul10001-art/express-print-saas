"""
app/services/dhl_service.py's payload construction and response parsing -
mocked against httpx.post (never a real DHL call, see that module's own
docstring on why: api-sandbox.dhl.com is unreachable from this environment).
These tests check that this app's own logic builds the request DHL's real
OpenAPI spec expects (profile field, ISO alpha-3 country codes, street/house
splitting) and parses a realistically-shaped response correctly - not that
DHL itself accepts the request.
"""

import uuid
from decimal import Decimal

import pytest

from app.core.crypto import encrypt_token
from app.models.dhl_account import DhlAccount
from app.models.order import Order
from app.services.dhl_service import (
    DhlShipmentError,
    _split_street_and_house,
    _to_alpha3,
    create_shipment_label,
)


def _make_dhl_account(**overrides):
    defaults = dict(
        billing_number="22222222220101",
        api_username_encrypted=encrypt_token("user-valid"),
        api_password_encrypted=encrypt_token("SandboxPasswort2023!"),
        sender_name="Test Sender GmbH",
        sender_street="Teststraße 1",
        sender_zip="10115",
        sender_city="Berlin",
        sender_country="DE",
        environment="SANDBOX",
    )
    defaults.update(overrides)
    return DhlAccount(**defaults)


def _make_order(**overrides):
    defaults = dict(
        id=uuid.uuid4(),
        recipient_name="Max Mustermann",
        recipient_street1="Musterstraße 12a",
        recipient_street2=None,
        recipient_city="München",
        recipient_state=None,
        recipient_zip="80331",
        recipient_country_code="DE",
        recipient_phone="+491234567890",
    )
    defaults.update(overrides)
    return Order(**defaults)


class _FakeResponse:
    def __init__(self, status_code, json_body):
        self.status_code = status_code
        self._json_body = json_body
        self.text = str(json_body)

    def json(self):
        return self._json_body


def test_to_alpha3_converts_known_codes():
    assert _to_alpha3("DE") == "DEU"
    assert _to_alpha3("de") == "DEU"
    assert _to_alpha3("AT") == "AUT"


def test_to_alpha3_passes_through_unknown_codes():
    assert _to_alpha3("XX") == "XX"
    assert _to_alpha3(None) == "DEU"  # falls back to "DE" -> "DEU"


def test_split_street_and_house_extracts_trailing_number():
    assert _split_street_and_house("Hauptstraße 12") == ("Hauptstraße", "12")
    assert _split_street_and_house("Hauptstraße 12a") == ("Hauptstraße", "12a")
    assert _split_street_and_house("Kurfürstendamm 200 A") == ("Kurfürstendamm", "200 A")


def test_split_street_and_house_falls_back_to_whole_string():
    assert _split_street_and_house("No House Number Here") == ("No House Number Here", None)
    assert _split_street_and_house(None) == ("", None)


def test_create_shipment_label_rejects_incomplete_address():
    account = _make_dhl_account()
    order = _make_order(recipient_street1=None)
    with pytest.raises(DhlShipmentError, match="Lieferadresse"):
        create_shipment_label(account, order, Decimal("1.0"), 20, 15, 10)


def test_create_shipment_label_rejects_unconnected_account():
    account = _make_dhl_account(billing_number=None, api_username_encrypted=None)
    order = _make_order()
    with pytest.raises(DhlShipmentError, match="Kein verbundenes DHL-Konto"):
        create_shipment_label(account, order, Decimal("1.0"), 20, 15, 10)


def test_create_shipment_label_builds_payload_matching_dhl_spec(monkeypatch):
    """Verifies the actual request sent - profile field present, country
    codes converted to ISO alpha-3, and the street/house number split -
    all three were bugs relative to DHL's real OpenAPI spec before this."""
    account = _make_dhl_account()
    order = _make_order()

    captured = {}

    def fake_post(url, json, auth, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["auth"] = auth
        captured["headers"] = headers
        return _FakeResponse(201, {
            "items": [{"shipmentNo": "SHIP123", "label": {"b64": "aGVsbG8="}}],
        })

    monkeypatch.setattr("app.services.dhl_service.httpx.post", fake_post)

    result = create_shipment_label(account, order, Decimal("1.5"), 20, 15, 10)

    assert captured["url"].endswith("/orders")
    assert captured["auth"] == ("user-valid", "SandboxPasswort2023!")
    assert captured["headers"]["DHL-API-Key"]

    payload = captured["json"]
    assert payload["profile"] == "STANDARD_GRUPPENPROFIL"
    shipment = payload["shipments"][0]
    assert shipment["shipper"]["country"] == "DEU"
    assert shipment["shipper"]["addressStreet"] == "Teststraße"
    assert shipment["shipper"]["addressHouse"] == "1"
    assert shipment["consignee"]["country"] == "DEU"
    assert shipment["consignee"]["addressStreet"] == "Musterstraße"
    assert shipment["consignee"]["addressHouse"] == "12a"
    assert shipment["details"]["weight"]["value"] == 1.5

    assert result.shipment_number == "SHIP123"
    assert result.tracking_number == "SHIP123"
    assert result.label_pdf == b"hello"


def test_create_shipment_label_raises_on_dhl_rejection(monkeypatch):
    account = _make_dhl_account()
    order = _make_order()

    monkeypatch.setattr(
        "app.services.dhl_service.httpx.post",
        lambda *a, **kw: _FakeResponse(400, {"detail": "Invalid billing number"}),
    )

    with pytest.raises(DhlShipmentError, match="Status 400"):
        create_shipment_label(account, order, Decimal("1.0"), 20, 15, 10)


def test_create_shipment_label_raises_when_no_label_in_response(monkeypatch):
    account = _make_dhl_account()
    order = _make_order()

    monkeypatch.setattr(
        "app.services.dhl_service.httpx.post",
        lambda *a, **kw: _FakeResponse(201, {
            "items": [{"validationMessages": [{"property": "shipper.country", "validationMessage": "invalid"}]}],
        }),
    )

    with pytest.raises(DhlShipmentError, match="kein Versandetikett"):
        create_shipment_label(account, order, Decimal("1.0"), 20, 15, 10)


def test_create_shipment_label_raises_on_empty_items(monkeypatch):
    account = _make_dhl_account()
    order = _make_order()

    monkeypatch.setattr("app.services.dhl_service.httpx.post", lambda *a, **kw: _FakeResponse(201, {"items": []}))

    with pytest.raises(DhlShipmentError, match="ohne Sendungsdaten"):
        create_shipment_label(account, order, Decimal("1.0"), 20, 15, 10)
