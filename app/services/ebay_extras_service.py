"""Read-only eBay extras: shipping policies, listing traffic, returns /
cancellations / inquiries / payment disputes, and buyer messages.

Everything here only READS from eBay. Each call is wrapped so one failing
endpoint (missing scope, API not enabled for the keyset, ...) never breaks
the others - the error text is returned next to the data instead.
"""

import xml.etree.ElementTree as ET
from datetime import date, timedelta

import httpx
from sqlalchemy.orm import Session

from app.models.ebay_account import EbayAccount
from app.services import ebay_oauth_service

API = {"SANDBOX": "https://api.sandbox.ebay.com", "PRODUCTION": "https://api.ebay.com"}
APIZ = {"SANDBOX": "https://apiz.sandbox.ebay.com", "PRODUCTION": "https://apiz.ebay.com"}
TRADING = {"SANDBOX": "https://api.sandbox.ebay.com/ws/api.dll", "PRODUCTION": "https://api.ebay.com/ws/api.dll"}
NS = {"e": "urn:ebay:apis:eBLBaseComponents"}
MARKETPLACE = "EBAY_DE"


def _get_json(url: str, headers: dict, params: dict | None = None) -> dict:
    resp = httpx.get(url, headers=headers, params=params, timeout=20.0)
    if resp.status_code != 200:
        raise ValueError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE}


# --- Shipping policies (sell.account) -------------------------------------

def shipping_policies(account: EbayAccount, db: Session) -> list[dict]:
    token = ebay_oauth_service.get_valid_access_token(account, db)
    data = _get_json(f"{API[account.environment]}/sell/account/v1/fulfillment_policy",
                     _bearer(token), {"marketplace_id": MARKETPLACE})
    out = []
    for policy in data.get("fulfillmentPolicies", []):
        services = []
        for option in policy.get("shippingOptions", []):
            for svc in option.get("shippingServices", []):
                cost = (svc.get("shippingCost") or {}).get("value")
                services.append({"service": svc.get("shippingServiceCode"), "cost": cost,
                                 "free": bool(svc.get("freeShipping"))})
        out.append({"name": policy.get("name"), "handling_days": (policy.get("handlingTime") or {}).get("value"),
                    "services": services})
    return out


# --- Seller subscriptions (sell.account) ----------------------------------

def subscriptions(account: EbayAccount, db: Session) -> dict:
    """Raw answer of the Account API getSubscription: the seller's eBay Store subscription
    (level, status, terms). Read-only. Never raises: eBay's HTTP error text is returned instead
    (an account without a subscription may answer with an error or an empty list)."""
    token = ebay_oauth_service.get_valid_access_token(account, db)
    resp = httpx.get(f"{API[account.environment]}/sell/account/v1/subscription",
                     headers=_bearer(token), timeout=20.0)
    if resp.status_code != 200:
        return {"ok": False, "http_status": resp.status_code, "error": resp.text[:300]}
    return {"ok": True, "http_status": 200, "data": resp.json()}


# --- Listing traffic (sell.analytics.readonly) ----------------------------

def traffic(account: EbayAccount, db: Session, days: int = 30) -> list[dict]:
    token = ebay_oauth_service.get_valid_access_token(account, db)
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    params = {
        "dimension": "LISTING",
        "metric": "LISTING_VIEWS_TOTAL,TRANSACTION,SALES_CONVERSION_RATE",
        "filter": f"marketplace_ids:{{{MARKETPLACE}}},date_range:[{start:%Y%m%d}..{end:%Y%m%d}]",
    }
    data = _get_json(f"{API[account.environment]}/sell/analytics/v1/traffic_report", _bearer(token), params)
    names = [(m.get("metric") or "") for m in (data.get("header") or {}).get("metrics", [])]
    rows = []
    for record in data.get("records", []):
        values = {names[i]: m.get("value") for i, m in enumerate(record.get("metricValues", [])) if i < len(names)}
        dim = (record.get("dimensionValues") or [{}])[0]
        rows.append({"item_id": dim.get("value"), "views": values.get("LISTING_VIEWS_TOTAL"),
                     "sales": values.get("TRANSACTION"), "conversion": values.get("SALES_CONVERSION_RATE")})
    rows.sort(key=lambda r: float(r["views"] or 0), reverse=True)
    return rows


# --- Returns / cancellations / inquiries / disputes -----------------------

def _post_order_headers(token: str) -> dict:
    return {"Authorization": f"IAF {token}", "Content-Type": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": MARKETPLACE}


def cases(account: EbayAccount, db: Session) -> dict:
    """{'items': [...], 'errors': [...]} - one entry per return/cancellation/inquiry/dispute."""
    token = ebay_oauth_service.get_valid_access_token(account, db)
    base, basez = API[account.environment], APIZ[account.environment]
    items, errors = [], []

    def run(label, fn):
        try:
            items.extend(fn())
        except (ValueError, httpx.HTTPError) as exc:
            errors.append(f"{label}: {exc}")

    def returns():
        data = _get_json(f"{base}/post-order/v2/return/search", _post_order_headers(token), {"limit": 25})
        return [{"type": "return", "id": r.get("returnId"), "status": r.get("state"),
                 "buyer": r.get("buyerLoginName"), "date": (r.get("creationInfo") or {}).get("creationDate", {}).get("value"),
                 "detail": (r.get("creationInfo") or {}).get("reason")} for r in data.get("members", [])]

    def cancellations():
        data = _get_json(f"{base}/post-order/v2/cancellation/search", _post_order_headers(token), {"limit": 25})
        return [{"type": "cancellation", "id": c.get("cancelId"), "status": c.get("cancelState"),
                 "buyer": c.get("buyerLoginName"), "date": (c.get("cancelRequestDate") or {}).get("value"),
                 "detail": c.get("cancelReason")} for c in data.get("cancellations", [])]

    def inquiries():
        data = _get_json(f"{base}/post-order/v2/inquiry/search", _post_order_headers(token), {"limit": 25})
        return [{"type": "inquiry", "id": i.get("inquiryId"), "status": i.get("inquiryState"),
                 "buyer": i.get("buyer"), "date": (i.get("creationDate") or {}).get("value"),
                 "detail": i.get("inquiryType")} for i in data.get("members", [])]

    def disputes():
        data = _get_json(f"{basez}/sell/fulfillment/v1/payment_dispute_summary", _bearer(token), {"limit": 25})
        return [{"type": "dispute", "id": d.get("paymentDisputeId"), "status": d.get("paymentDisputeStatus"),
                 "buyer": d.get("buyerUsername"), "date": d.get("openDate"), "detail": d.get("reason")}
                for d in data.get("paymentDisputeSummaries", [])]

    run("Rückgaben", returns)
    run("Stornierungen", cancellations)
    run("Anfragen", inquiries)
    run("Zahlungsstreit", disputes)
    return {"items": items, "errors": errors}


# --- Buyer messages (Trading API GetMyMessages) ---------------------------

def _trading(account: EbayAccount, token: str, call: str, body: str) -> ET.Element:
    xml = (f'<?xml version="1.0" encoding="utf-8"?><{call}Request xmlns="urn:ebay:apis:eBLBaseComponents">'
           f"{body}</{call}Request>")
    resp = httpx.post(TRADING[account.environment], content=xml, timeout=30.0, headers={
        "X-EBAY-API-IAF-TOKEN": token, "X-EBAY-API-SITEID": "77", "X-EBAY-API-COMPATIBILITY-LEVEL": "1193",
        "X-EBAY-API-CALL-NAME": call, "Content-Type": "text/xml"})
    if resp.status_code != 200:
        raise ValueError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    root = ET.fromstring(resp.text)
    ack = root.find("e:Ack", NS)
    if ack is not None and ack.text == "Failure":
        msg = root.find("e:Errors/e:LongMessage", NS)
        raise ValueError(msg.text if msg is not None else "eBay error")
    return root


def _t(node, path):
    found = node.find(path, NS)
    return found.text.strip() if found is not None and found.text else None


MESSAGE_BATCH = 10  # GetMyMessages rejects more MessageIDs than this per call


def messages(account: EbayAccount, db: Session, limit: int = 20) -> list[dict]:
    token = ebay_oauth_service.get_valid_access_token(account, db)
    headers_root = _trading(account, token, "GetMyMessages",
                            "<DetailLevel>ReturnHeaders</DetailLevel><FolderID>0</FolderID>")
    heads = headers_root.findall("e:Messages/e:Message", NS)[:limit]
    ids = [_t(h, "e:MessageID") for h in heads if _t(h, "e:MessageID")]
    if not ids:
        return []
    out = []
    for start in range(0, len(ids), MESSAGE_BATCH):
        id_xml = "".join(f"<MessageID>{i}</MessageID>" for i in ids[start:start + MESSAGE_BATCH])
        full = _trading(account, token, "GetMyMessages",
                        f"<DetailLevel>ReturnMessages</DetailLevel><MessageIDs>{id_xml}</MessageIDs>")
        for m in full.findall("e:Messages/e:Message", NS):
            # only the plain text: "Content" is eBay's whole HTML mail, which also
            # carries names and order data and must never end up in the AI box
            out.append({"id": _t(m, "e:MessageID"), "sender": _t(m, "e:Sender"), "subject": _t(m, "e:Subject"),
                        "text": _t(m, "e:Text"), "date": _t(m, "e:ReceiveDate"),
                        "item_id": _t(m, "e:ItemID"), "read": _t(m, "e:Read") == "true"})
    return out
