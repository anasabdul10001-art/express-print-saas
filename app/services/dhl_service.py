"""
Calls DHL's Parcel DE Shipping API (Post & Parcel Germany v2) to create a
real shipment and buy a label, for a tenant who has connected their own DHL
Business Customer account (see app/models/dhl_account.py, app/routers/dhl.py).

*** UNVERIFIED AGAINST A LIVE SANDBOX - READ BEFORE RELYING ON THIS FILE ***
developer.dhl.com is unreachable from this environment's network, so the
request/response shape below (billingNumber/shipper/consignee fields, the
"V01PAK" product code, and the items[].label.b64 / items[].shipmentNo
response fields) comes from DHL's public docs and third-party integration
write-ups found via web search, NOT from an actual sandbox call. Treat the
first real call made with a real DHL_API_KEY + a tenant's real sandbox
account as a live test of this file's assumptions, not just of the
credentials - check the actual response shape against what's assumed here
(see _parse_response below) before trusting it in production.
"""

import base64
from dataclasses import dataclass
from decimal import Decimal

import httpx

from app.config import settings
from app.core.crypto import decrypt_token
from app.models.dhl_account import DhlAccount
from app.models.order import Order

DHL_API_BASE = {
    "SANDBOX": "https://api-sandbox.dhl.com/parcel/de/shipping/v2",
    "PRODUCTION": "https://api-eu.dhl.com/parcel/de/shipping/v2",
}

# Standard DHL Paket, national - the only product this integration currently
# requests. DHL has many other product codes (Warenpost, international
# Paket, Päckchen, ...) that a later phase could let the tenant choose.
DEFAULT_PRODUCT_CODE = "V01PAK"


class DhlShipmentError(Exception):
    """Raised when the DHL account isn't usable, the order can't be shipped
    yet, or DHL itself rejects/fails the request."""


@dataclass
class DhlShipmentResult:
    label_pdf: bytes
    shipment_number: str | None
    tracking_number: str | None


def create_shipment_label(
    dhl_account: DhlAccount,
    order: Order,
    weight_kg: Decimal,
    length_cm: int,
    width_cm: int,
    height_cm: int,
) -> DhlShipmentResult:
    """
    Creates one DHL shipment order for `order` and returns its label as raw
    PDF bytes. Raises DhlShipmentError on any problem - callers (see
    app/routers/print_jobs.py) are expected to surface that as a clear error
    to the seller rather than silently falling back to a placeholder label,
    since a "successful" print of the wrong label would be worse than a
    visible failure.
    """
    if not order.recipient_street1 or not order.recipient_city or not order.recipient_zip:
        raise DhlShipmentError(
            "Für diese Bestellung fehlt eine vollständige Lieferadresse - "
            "DHL-Versandetikett kann nicht erstellt werden."
        )
    if not dhl_account.billing_number or not dhl_account.api_username_encrypted:
        raise DhlShipmentError("Kein verbundenes DHL-Konto für dieses Konto gefunden.")

    username = decrypt_token(dhl_account.api_username_encrypted)
    password = decrypt_token(dhl_account.api_password_encrypted)
    base_url = DHL_API_BASE.get(dhl_account.environment, DHL_API_BASE["SANDBOX"])

    payload = {
        "shipments": [
            {
                "product": DEFAULT_PRODUCT_CODE,
                "billingNumber": dhl_account.billing_number,
                "refNo": str(order.id),
                "shipper": {
                    "name1": dhl_account.sender_name,
                    "addressStreet": dhl_account.sender_street,
                    "postalCode": dhl_account.sender_zip,
                    "city": dhl_account.sender_city,
                    "country": dhl_account.sender_country,
                },
                "consignee": {
                    "name1": order.recipient_name or "Empfänger",
                    "addressStreet": order.recipient_street1,
                    "additionalAddressInformation1": order.recipient_street2,
                    "postalCode": order.recipient_zip,
                    "city": order.recipient_city,
                    "state": order.recipient_state,
                    "country": order.recipient_country_code or "DE",
                    "phone": order.recipient_phone,
                },
                "details": {
                    "weight": {"uom": "kg", "value": float(weight_kg)},
                    "dim": {"uom": "cm", "length": length_cm, "width": width_cm, "height": height_cm},
                },
            }
        ],
    }

    try:
        response = httpx.post(
            f"{base_url}/orders",
            json=payload,
            auth=(username, password),
            headers={
                "DHL-API-Key": settings.dhl_api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        raise DhlShipmentError(f"DHL ist nicht erreichbar: {exc}") from exc

    if response.status_code not in (200, 201):
        raise DhlShipmentError(f"DHL hat die Anfrage abgelehnt (Status {response.status_code}): {response.text}")

    return _parse_response(response.json())


def _parse_response(body: dict) -> DhlShipmentResult:
    """
    Split out from create_shipment_label so the (unverified) response
    parsing is easy to find and fix in one place once a real sandbox
    response is available to compare against.
    """
    items = body.get("items") or []
    if not items:
        raise DhlShipmentError(f"Unerwartete DHL-Antwort ohne Sendungsdaten: {body}")

    item = items[0]
    label = item.get("label") or {}
    label_b64 = label.get("b64")
    if not label_b64:
        validation_messages = item.get("validationMessages")
        detail = validation_messages if validation_messages else item
        raise DhlShipmentError(f"DHL hat kein Versandetikett zurückgegeben: {detail}")

    shipment_number = item.get("shipmentNo")

    return DhlShipmentResult(
        label_pdf=base64.b64decode(label_b64),
        shipment_number=shipment_number,
        tracking_number=shipment_number,
    )
