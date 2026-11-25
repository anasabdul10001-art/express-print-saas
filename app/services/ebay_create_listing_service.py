"""
Creates a new fixed-price listing on eBay.de with the Trading API.

verify() uses VerifyAddFixedPriceItem: eBay checks the listing and reports errors
and fees, but nothing is listed. publish() uses AddFixedPriceItem and really lists
(or schedules) the item; it is only called after the seller confirmed it, and is
never retried. Item.UUID makes eBay reject a second submission of the same draft.

Shipping, return and payment terms come from the seller's eBay business policies.
Not handled here: variations, GPSR / WEEE fields, auctions, picture upload (the
pictures must already be reachable by https).
"""

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from xml.sax.saxutils import escape

import httpx
from sqlalchemy.orm import Session

from app.models.ebay_account import EbayAccount
from app.services import ebay_oauth_service
from app.services.ebay_extras_service import API, MARKETPLACE
from app.services.ebay_listing_service import NS, TRADING_URL, _headers, _text

POLICY_KINDS = {
    "shipping": ("fulfillment_policy", "fulfillmentPolicies", "fulfillmentPolicyId"),
    "return": ("return_policy", "returnPolicies", "returnPolicyId"),
    "payment": ("payment_policy", "paymentPolicies", "paymentPolicyId"),
}


class PublishUncertain(Exception):
    """No usable answer from eBay: the listing may or may not exist."""


def business_policies(account: EbayAccount, db: Session) -> dict:
    token = ebay_oauth_service.get_valid_access_token(account, db)
    out = {}
    for kind, (path, list_key, id_key) in POLICY_KINDS.items():
        response = httpx.get(f"{API[account.environment]}/sell/account/v1/{path}",
                             headers={"Authorization": f"Bearer {token}"},
                             params={"marketplace_id": MARKETPLACE}, timeout=20.0)
        if response.status_code != 200:
            raise ValueError(f"eBay-Rahmenbedingungen: HTTP {response.status_code} {response.text[:200]}")
        out[kind] = [{"id": p.get(id_key), "name": p.get("name")} for p in response.json().get(list_key, [])]
    return out


def _tag(name: str, value) -> str:
    return f"<{name}>{escape(str(value))}</{name}>"


def build_item_xml(call: str, draft: dict) -> str:
    """draft: already validated by the router."""
    item = [
        _tag("UUID", draft["draft_id"].replace("-", "").upper()),
        _tag("Title", draft["title"]),
        # The description is HTML for eBay; plain text is turned into safe HTML first.
        _tag("Description", escape(draft["description"]).replace("\n", "<br>")),
        f"<PrimaryCategory>{_tag('CategoryID', draft['category_id'])}</PrimaryCategory>",
        _tag("StartPrice", f"{draft['price']:.2f}"),
        _tag("Quantity", draft["quantity"]),
        _tag("ConditionID", draft["condition_id"]),
        "<Country>DE</Country><Currency>EUR</Currency>",
        _tag("Location", draft["location"]),
        _tag("PostalCode", draft["postal_code"]),
        "<ListingType>FixedPriceItem</ListingType><ListingDuration>GTC</ListingDuration>",
        "<PictureDetails>" + "".join(_tag("PictureURL", u) for u in draft["picture_urls"]) + "</PictureDetails>",
        "<SellerProfiles>"
        f"<SellerShippingProfile>{_tag('ShippingProfileID', draft['shipping_policy_id'])}</SellerShippingProfile>"
        f"<SellerReturnProfile>{_tag('ReturnProfileID', draft['return_policy_id'])}</SellerReturnProfile>"
        f"<SellerPaymentProfile>{_tag('PaymentProfileID', draft['payment_policy_id'])}</SellerPaymentProfile>"
        "</SellerProfiles>",
    ]
    if draft.get("sku"):
        item.append(_tag("SKU", draft["sku"]))
    if draft.get("specifics"):
        item.append("<ItemSpecifics>" + "".join(
            f"<NameValueList>{_tag('Name', n)}{_tag('Value', v)}</NameValueList>"
            for n, v in draft["specifics"].items()) + "</ItemSpecifics>")
    if draft.get("schedule_at"):
        when = draft["schedule_at"].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        item.append(_tag("ScheduleTime", when))
    return ('<?xml version="1.0" encoding="utf-8"?>'
            f'<{call}Request xmlns="urn:ebay:apis:eBLBaseComponents">'
            f"<ErrorLanguage>de_DE</ErrorLanguage><Item>{''.join(item)}</Item></{call}Request>")


def parse_response(xml_text: str) -> dict:
    root = ET.fromstring(xml_text)
    ack = _text(root, "e:Ack")
    messages = []
    for error in root.findall("e:Errors", NS):
        text = _text(error, "e:LongMessage") or _text(error, "e:ShortMessage")
        if text:
            messages.append({"severity": (_text(error, "e:SeverityCode") or "Error").lower(), "text": text})
    fees = []
    for fee in root.findall("e:Fees/e:Fee", NS):
        amount = _text(fee, "e:Fee")
        try:
            if amount and float(amount) > 0:
                fees.append({"name": _text(fee, "e:Name"), "amount": amount})
        except ValueError:
            pass
    return {"ok": ack in ("Success", "Warning"), "item_id": _text(root, "e:ItemID"),
            "messages": messages, "fees": fees}


def _call(account: EbayAccount, db: Session, call: str, draft: dict) -> dict:
    token = ebay_oauth_service.get_valid_access_token(account, db)
    response = httpx.post(TRADING_URL[account.environment], headers=_headers(token, call),
                          content=build_item_xml(call, draft).encode(), timeout=45.0)
    if response.status_code != 200:
        raise ValueError(f"eBay antwortet {response.status_code}: {response.text[:200]}")
    return parse_response(response.text)


def verify(account: EbayAccount, db: Session, draft: dict) -> dict:
    return _call(account, db, "VerifyAddFixedPriceItem", draft)


def publish(account: EbayAccount, db: Session, draft: dict) -> dict:
    # Get the token first: a failure there certainly happened before anything was sent.
    ebay_oauth_service.get_valid_access_token(account, db)
    try:
        return _call(account, db, "AddFixedPriceItem", draft)
    except (httpx.HTTPError, ET.ParseError, ValueError) as exc:
        raise PublishUncertain(str(exc)) from exc


def is_future(when: datetime) -> bool:
    return when.astimezone(timezone.utc) > datetime.now(timezone.utc)
