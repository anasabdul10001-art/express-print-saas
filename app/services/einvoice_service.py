"""
The structured e-invoice for a seller's invoice: UN/CEFACT Cross Industry Invoice
XML following EN 16931, for domestic sales in EUR. It is built from the same
snapshot as the PDF, so both show the same totals.

This is a standalone XML file (not a ZUGFeRD PDF with embedded XML, and without
the extra XRechnung rules that public authorities require).
"""

import re
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal

GUIDELINE = "urn:cen.eu:en16931:2017"
NS = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
    "qdt": "urn:un:unece:uncefact:data:standard:QualifiedDataType:100",
}
for _prefix, _uri in NS.items():
    ET.register_namespace(_prefix, _uri)

CENT = Decimal("0.01")
EXEMPTION_TEXT = "Gemäß § 19 UStG wird keine Umsatzsteuer berechnet."
# XML 1.0 forbids most control characters.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def parse_address(address: str) -> dict | None:
    """Splits the seller's free-text address; needs a line 'PLZ Ort' after the street."""
    lines = [line.strip() for line in (address or "").splitlines() if line.strip()]
    if lines and lines[-1].lower() in ("deutschland", "germany"):
        lines.pop()
    if len(lines) < 2:
        return None
    match = re.match(r"^(?:D-)?(\d{5})\s+(.+)$", lines[-1])
    if not match:
        return None
    return {"street1": lines[0], "street2": ", ".join(lines[1:-1]), "zip": match.group(1), "city": match.group(2)}


def _add(parent, tag: str, text: str | None = None, **attributes):
    prefix, name = tag.split(":")
    element = ET.SubElement(parent, f"{{{NS[prefix]}}}{name}", attributes)
    if text is not None:
        element.text = _CONTROL.sub(" ", text)
    return element


def _date(parent, tag: str, value: date, namespace: str = "udt"):
    _add(_add(parent, tag), f"{namespace}:DateTimeString", value.strftime("%Y%m%d"), format="102")


def _money(value: Decimal) -> str:
    return str(value.quantize(CENT))


def _line_nets(lines: list[dict], net_total: Decimal, rate: Decimal) -> list[Decimal]:
    """Net amount per line. The rounding remainder goes to the biggest line, so that the
    lines add up to the invoice's net amount exactly."""
    nets = [(Decimal(line["total"]) * 100 / (100 + rate)).quantize(CENT) for line in lines]
    biggest = max(range(len(nets)), key=lambda i: abs(nets[i]))
    nets[biggest] += net_total - sum(nets)
    return nets


def _party(parent, tag: str, name: str, address: dict, identifier: str | None = None):
    party = _add(parent, tag)
    if identifier:
        _add(party, "ram:ID", identifier)
    _add(party, "ram:Name", name)
    postal = _add(party, "ram:PostalTradeAddress")
    _add(postal, "ram:PostcodeCode", address["zip"])
    _add(postal, "ram:LineOne", address["street1"])
    if address.get("street2"):
        _add(postal, "ram:LineTwo", address["street2"])
    _add(postal, "ram:CityName", address["city"])
    _add(postal, "ram:CountryID", "DE")
    return party


def build_xml(snapshot: dict, number: str, issue_date: date) -> bytes | None:
    """None when the data isn't enough for an e-invoice (no complete buyer address, or a
    seller address without a 'PLZ Ort' line)."""
    seller = snapshot["seller"]
    seller_address = parse_address(seller["address"])
    buyer = snapshot.get("buyer_address")
    if not seller_address or not buyer:
        return None

    regular = snapshot["vat_mode"] == "regular"
    rate = Decimal(snapshot["vat_rate"]) if regular else Decimal("0")
    category = "S" if regular else "E"
    gross = Decimal(snapshot["gross"])
    net = Decimal(snapshot["net"]) if regular else gross
    vat = Decimal(snapshot["vat"]) if regular else Decimal("0")
    lines = snapshot["lines"]
    nets = _line_nets(lines, net, rate) if regular else [Decimal(line["total"]) for line in lines]
    cancels = snapshot.get("cancels")

    root = ET.Element(f"{{{NS['rsm']}}}CrossIndustryInvoice")
    context = _add(root, "rsm:ExchangedDocumentContext")
    _add(_add(context, "ram:GuidelineSpecifiedDocumentContextParameter"), "ram:ID", GUIDELINE)

    document = _add(root, "rsm:ExchangedDocument")
    _add(document, "ram:ID", number)
    _add(document, "ram:TypeCode", "384" if cancels else "380")  # 384 = corrected invoice
    _date(document, "ram:IssueDateTime", issue_date)

    transaction = _add(root, "rsm:SupplyChainTradeTransaction")
    for position, (line, line_net) in enumerate(zip(lines, nets), start=1):
        item = _add(transaction, "ram:IncludedSupplyChainTradeLineItem")
        _add(_add(item, "ram:AssociatedDocumentLineDocument"), "ram:LineID", str(position))
        _add(_add(item, "ram:SpecifiedTradeProduct"), "ram:Name", line["title"])
        # A price may not be negative, so a discount or cancellation has a negative quantity.
        quantity = -line["quantity"] if line_net < 0 else line["quantity"]
        price = (abs(line_net) / line["quantity"]).quantize(Decimal("0.0001"))
        agreement = _add(item, "ram:SpecifiedLineTradeAgreement")
        _add(_add(agreement, "ram:NetPriceProductTradePrice"), "ram:ChargeAmount", str(price))
        _add(_add(item, "ram:SpecifiedLineTradeDelivery"), "ram:BilledQuantity", str(quantity), unitCode="C62")
        settlement = _add(item, "ram:SpecifiedLineTradeSettlement")
        tax = _add(settlement, "ram:ApplicableTradeTax")
        _add(tax, "ram:TypeCode", "VAT")
        _add(tax, "ram:CategoryCode", category)
        _add(tax, "ram:RateApplicablePercent", str(rate))
        summation = _add(settlement, "ram:SpecifiedTradeSettlementLineMonetarySummation")
        _add(summation, "ram:LineTotalAmount", _money(line_net))

    agreement = _add(transaction, "ram:ApplicableHeaderTradeAgreement")
    # BR-CO-26: without a VAT ID the seller still needs an identifier (BT-29), so the tax number is used.
    seller_party = _party(agreement, "ram:SellerTradeParty", seller["name"], seller_address,
                          identifier=None if seller.get("vat_id") else seller.get("tax_number"))
    if seller.get("email"):
        _add(_add(seller_party, "ram:URIUniversalCommunication"), "ram:URIID", seller["email"], schemeID="EM")
    if seller.get("vat_id"):
        _add(_add(seller_party, "ram:SpecifiedTaxRegistration"), "ram:ID", seller["vat_id"].replace(" ", ""),
             schemeID="VA")
    if seller.get("tax_number"):
        _add(_add(seller_party, "ram:SpecifiedTaxRegistration"), "ram:ID", seller["tax_number"], schemeID="FC")
    _party(agreement, "ram:BuyerTradeParty", buyer["name"], buyer)
    order_ref = snapshot.get("order_ref")
    if order_ref and not order_ref.startswith("deleted:"):
        _add(_add(agreement, "ram:BuyerOrderReferencedDocument"), "ram:IssuerAssignedID", order_ref)

    delivery = _add(transaction, "ram:ApplicableHeaderTradeDelivery")
    _date(_add(delivery, "ram:ActualDeliverySupplyChainEvent"), "ram:OccurrenceDateTime",
          date.fromisoformat(snapshot["delivery_date"]))

    settlement = _add(transaction, "ram:ApplicableHeaderTradeSettlement")
    _add(settlement, "ram:InvoiceCurrencyCode", "EUR")
    tax = _add(settlement, "ram:ApplicableTradeTax")
    _add(tax, "ram:CalculatedAmount", _money(vat))
    _add(tax, "ram:TypeCode", "VAT")
    if not regular:
        _add(tax, "ram:ExemptionReason", EXEMPTION_TEXT)
    _add(tax, "ram:BasisAmount", _money(net))
    _add(tax, "ram:CategoryCode", category)
    _add(tax, "ram:RateApplicablePercent", str(rate))
    _add(_add(settlement, "ram:SpecifiedTradePaymentTerms"), "ram:Description", snapshot["payment_terms"])
    totals = _add(settlement, "ram:SpecifiedTradeSettlementHeaderMonetarySummation")
    _add(totals, "ram:LineTotalAmount", _money(net))
    _add(totals, "ram:TaxBasisTotalAmount", _money(net))
    _add(totals, "ram:TaxTotalAmount", _money(vat), currencyID="EUR")
    _add(totals, "ram:GrandTotalAmount", _money(gross))
    _add(totals, "ram:DuePayableAmount", _money(gross))
    if cancels:
        reference = _add(settlement, "ram:InvoiceReferencedDocument")
        _add(reference, "ram:IssuerAssignedID", cancels["number"])
        _date(reference, "ram:FormattedIssueDateTime", date.fromisoformat(cancels["date"]), "qdt")

    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)
