"""
Deutsche Post INTERNETMARKE REST API (v1.30).

Request and response shapes follow the reference documentation on
developer.dhl.com. NOT YET VERIFIED against a live call: that needs approved
app credentials and a developer Portokasse. Points the documentation leaves open
are marked "unverified".
"""

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.config import settings

BASE_URL = "https://api-eu.dhl.com/post/de/shipping/im/v1"
# The label is downloaded from a link in the answer; only follow it to Deutsche Post / DHL.
LABEL_HOSTS = ("deutschepost.de", "dhl.com", "dhl.de")
MAX_LABEL_BYTES = 5 * 1024 * 1024
# Answers that clearly mean "nothing was bought".
REJECTED = (400, 401, 403, 404, 415, 422, 429)


class InternetmarkeError(Exception):
    """Nothing was charged."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class InternetmarkeOutcomeUnknown(InternetmarkeError):
    """The checkout was sent, but whether money was taken is not known."""


@dataclass
class Session:
    token: str
    wallet_cents: int | None


@dataclass
class Checkout:
    label_link: str
    voucher_id: str | None
    track_id: str | None
    wallet_cents: int | None


def is_configured() -> bool:
    return bool(settings.internetmarke_client_id and settings.internetmarke_client_secret)


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        return str(body.get("detail") or body.get("title") or "")[:300]
    except (ValueError, AttributeError):
        return ""


def _call(method: str, path: str, **kwargs) -> dict:
    """For requests that cost nothing."""
    try:
        response = httpx.request(method, f"{BASE_URL}{path}", timeout=30.0, **kwargs)
    except httpx.HTTPError as exc:
        raise InternetmarkeError("Die Deutsche Post ist nicht erreichbar.") from exc
    if response.status_code != 200:
        raise InternetmarkeError(
            f"Die Deutsche Post hat die Anfrage abgelehnt (Status {response.status_code}). {_detail(response)}".strip(),
            response.status_code)
    try:
        return response.json()
    except ValueError as exc:
        raise InternetmarkeError("Unlesbare Antwort der Deutschen Post.") from exc


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def login(username: str, password: str) -> Session:
    if not is_configured():
        raise InternetmarkeError("INTERNETMARKE ist auf diesem Server noch nicht eingerichtet.")
    try:
        body = _call("POST", "/user", data={
            "grant_type": "client_credentials", "username": username, "password": password,
            "client_id": settings.internetmarke_client_id, "client_secret": settings.internetmarke_client_secret})
    except InternetmarkeError as exc:
        if exc.status_code == 401:
            raise InternetmarkeError(
                "Anmeldung abgelehnt. Prüfe E-Mail und Passwort deiner Portokasse und gib NettoDesk dort unter "
                "\"Meine Daten > Geschäftsanwendungen\" frei.", 401) from exc
        raise
    if not body.get("access_token"):
        raise InternetmarkeError("Die Deutsche Post hat kein Zugriffstoken geliefert.")
    # walletBalance is taken to be in cents (unverified).
    return Session(token=body["access_token"], wallet_cents=body.get("walletBalance"))


def page_formats(session: Session) -> list[dict]:
    """Formats that can carry the receiver's address."""
    body = _call("GET", "/app/catalog", params={"types": "PAGE_FORMATS"}, headers=_bearer(session.token))
    return [{"id": f["id"], "name": f.get("name") or str(f["id"]), "description": f.get("description") or ""}
            for f in body.get("pageFormats") or [] if f.get("isAddressPossible")]


def create_cart(session: Session) -> str:
    shop_order_id = _call("POST", "/app/shoppingcart", headers=_bearer(session.token)).get("shopOrderId")
    if not shop_order_id:
        raise InternetmarkeError("Die Deutsche Post hat keine Bestellnummer geliefert.")
    return str(shop_order_id)


def checkout(session: Session, shop_order_id: str, product_code: int, price_cents: int,
             page_format_id: int, sender: dict, receiver: dict) -> Checkout:
    """Spends money. Only a clear rejection raises plain InternetmarkeError."""
    payload = {
        "type": "AppShoppingCartPDFRequest",
        "shopOrderId": shop_order_id,
        "total": price_cents,  # cents (unverified)
        "createManifest": False,
        "createShippingList": "0",
        "dpi": "DPI300",
        "pageFormatId": page_format_id,
        "positions": [{
            "positionType": "AppShoppingCartPDFPosition",
            "productCode": product_code,
            "address": {"sender": sender, "receiver": receiver},
            "voucherLayout": "ADDRESS_ZONE",
            # First field of the first page; counting from 1 is unverified.
            "position": {"labelX": 1, "labelY": 1, "page": 1},
        }],
    }
    try:
        response = httpx.post(f"{BASE_URL}/app/shoppingcart/pdf", params={"directCheckout": "true"},
                              json=payload, headers=_bearer(session.token), timeout=60.0)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise InternetmarkeError("Die Deutsche Post ist nicht erreichbar.") from exc
    except httpx.HTTPError as exc:
        raise InternetmarkeOutcomeUnknown("Die Verbindung brach während des Kaufs ab.") from exc
    if response.status_code in REJECTED:
        raise InternetmarkeError(
            f"Kauf abgelehnt (Status {response.status_code}). {_detail(response)}".strip(), response.status_code)
    if response.status_code != 200:
        raise InternetmarkeOutcomeUnknown(f"Unklare Antwort der Deutschen Post (Status {response.status_code}).")
    try:
        body = response.json()
        link = body["link"]
        voucher = ((body.get("shoppingCart") or {}).get("voucherList") or [{}])[0]
        voucher_id, track_id = voucher.get("voucherId"), voucher.get("trackId")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise InternetmarkeOutcomeUnknown("Unlesbare Antwort auf den Kauf.") from exc
    # "walletBallance" is spelled like this in the documentation.
    return Checkout(label_link=link, voucher_id=voucher_id, track_id=track_id,
                    wallet_cents=body.get("walletBallance"))


def download_label(link: str) -> bytes:
    parsed = urlparse(link or "")
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(host == h or host.endswith(f".{h}") for h in LABEL_HOSTS):
        raise InternetmarkeError("Unerwartete Adresse für das Etikett.")
    try:
        response = httpx.get(link, timeout=30.0)
    except httpx.HTTPError as exc:
        raise InternetmarkeError("Das Etikett konnte nicht geladen werden.") from exc
    if (response.status_code != 200 or not response.content.startswith(b"%PDF")
            or len(response.content) > MAX_LABEL_BYTES):
        raise InternetmarkeError("Das Etikett konnte nicht geladen werden.")
    return response.content
