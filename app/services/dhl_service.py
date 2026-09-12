"""
Calls DHL's Parcel DE Shipping API (Post & Parcel Germany v2) to create a
real shipment and buy a label, for a tenant who has connected their own DHL
Business Customer account (see app/models/dhl_account.py, app/routers/dhl.py).

*** STILL UNVERIFIED AGAINST A LIVE CALL - READ BEFORE RELYING ON THIS FILE ***
api-sandbox.dhl.com and developer.dhl.com are both unreachable from this
environment's network (blocked by egress policy, confirmed directly - not
just "didn't try"), so no live call has been made from here. However, the
request/response shape below (billingNumber/shipper/consignee fields, the
"profile" field, the "V01PAK" product code, and the items[].label.b64 /
items[].shipmentNo response fields) has been cross-checked against DHL's
own published OpenAPI spec (Parcel DE Shipping v2.1.10) rather than only
blog write-ups, which is a meaningfully higher confidence level than the
previous version of this file had - but "matches the spec" is still not
"confirmed working against a real sandbox response". Known real sandbox
test credentials (from DHL's own docs): username "user-valid", password
"SandboxPasswort2023!", billing numbers "22222222220801"/"22222222220101" -
connect those via the app's own DHL settings UI and trigger one real print
job as the actual live test, then fix _parse_response below against
whatever DHL's real response turns out to look like.
"""

import base64
import re
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

# Required at the root of every /orders request - controls which billing
# numbers the request is even allowed to use. DHL's own docs say to use
# this standard profile whenever no dedicated one has been set up, which is
# every tenant here (nothing in this app ever configures a custom one).
DEFAULT_PROFILE = "STANDARD_GRUPPENPROFIL"

# DHL's API requires ISO 3166-1 ALPHA-3 country codes ("DEU", not "DE"), but
# every country code already stored in this app (DhlAccount.sender_country,
# Order.recipient_country_code) is ISO alpha-2, matching eBay's own data and
# the rest of this codebase - so the conversion happens only here, at the
# DHL-payload boundary, rather than changing what's stored everywhere else.
# Covers the EU/EEA + UK/CH + a few other common shipping destinations;
# extend as needed if tenants ship further afield.
_ALPHA2_TO_ALPHA3 = {
    "DE": "DEU", "AT": "AUT", "CH": "CHE", "FR": "FRA", "NL": "NLD", "BE": "BEL",
    "LU": "LUX", "PL": "POL", "CZ": "CZE", "DK": "DNK", "IT": "ITA", "ES": "ESP",
    "GB": "GBR", "IE": "IRL", "SE": "SWE", "PT": "PRT", "FI": "FIN", "NO": "NOR",
    "HU": "HUN", "SK": "SVK", "SI": "SVN", "HR": "HRV", "RO": "ROU", "BG": "BGR",
    "GR": "GRC", "EE": "EST", "LV": "LVA", "LT": "LTU", "MT": "MLT", "CY": "CYP",
    "US": "USA", "CA": "CAN", "AU": "AUS", "CN": "CHN", "JP": "JPN",
}

# Best-effort split of a German-style "Straße Hausnummer" line (e.g.
# "Hauptstraße 12a") into DHL's separate addressStreet/addressHouse fields -
# neither this app's own address fields nor eBay's shipping address data
# keep the house number separate, so this is inferred rather than stored.
_HOUSE_NUMBER_RE = re.compile(r"^(.*?)\s+(\d+\s*[a-zA-Z]?)$")


def _to_alpha3(country_code: str | None) -> str:
    code = (country_code or "DE").upper()
    return _ALPHA2_TO_ALPHA3.get(code, code)


def _split_street_and_house(full_street: str | None) -> tuple[str, str | None]:
    full_street = (full_street or "").strip()
    match = _HOUSE_NUMBER_RE.match(full_street)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return full_street, None


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

    shipper_street, shipper_house = _split_street_and_house(dhl_account.sender_street)
    consignee_street, consignee_house = _split_street_and_house(order.recipient_street1)

    payload = {
        "profile": DEFAULT_PROFILE,
        "shipments": [
            {
                "product": DEFAULT_PRODUCT_CODE,
                "billingNumber": dhl_account.billing_number,
                "refNo": str(order.id),
                "shipper": {
                    "name1": dhl_account.sender_name,
                    "addressStreet": shipper_street,
                    "addressHouse": shipper_house,
                    "postalCode": dhl_account.sender_zip,
                    "city": dhl_account.sender_city,
                    "country": _to_alpha3(dhl_account.sender_country),
                },
                "consignee": {
                    "name1": order.recipient_name or "Empfänger",
                    "addressStreet": consignee_street,
                    "addressHouse": consignee_house,
                    "additionalAddressInformation1": order.recipient_street2,
                    "postalCode": order.recipient_zip,
                    "city": order.recipient_city,
                    "state": order.recipient_state,
                    "country": _to_alpha3(order.recipient_country_code),
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
