"""Filling help for the VAT advance return: the figures per form line, what is
still missing, and the entries the seller makes from their invoices."""

import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.expense import Expense
from app.models.order import Order
from app.models.purchase import Purchase
from app.models.tenant import Tenant
from app.models.user import User
from app.models.vat_period_input import VatPeriodInput
from app.services import ustva_service as svc

router = APIRouter(prefix="/accounting", tags=["accounting"])

ZERO = Decimal("0")


def _check_period(year: int, period: str) -> None:
    if year != svc.FORM_YEAR:
        raise HTTPException(status_code=400, detail=f"Die Ausfüllhilfe gibt es nur für das Formular {svc.FORM_YEAR}.")
    if period not in svc.PERIODS:
        raise HTTPException(status_code=400, detail="Unbekannter Zeitraum.")


def _text(value):
    return None if value is None else str(value)


@router.get("/ustva")
def ustva(
    period: str,
    year: int = Query(svc.FORM_YEAR),
    advance_payment: Decimal = Query(ZERO, ge=0, le=1000000, decimal_places=2),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _check_period(year, period)
    start, end = svc.period_range(period)
    tenant_id = current_user.tenant_id
    regular = db.query(Tenant.vat_mode).filter(Tenant.id == tenant_id).scalar() == "regular"

    sums: dict[str, Decimal] = defaultdict(lambda: ZERO)
    items, open_points = [], []
    input_vat = ZERO
    unclassified = without_vat = 0

    def item(kind, obj, day, title, amount):
        nonlocal input_vat, unclassified, without_vat
        treatment = obj.tax_treatment
        if not treatment:
            unclassified += 1
        elif treatment == "de_vat":
            if obj.vat_amount is None:
                without_vat += 1
            else:
                input_vat += obj.vat_amount
        else:
            sums[treatment] += amount
        items.append({"kind": kind, "id": str(obj.id), "date": day.isoformat(), "title": title,
                      "amount": str(amount), "tax_treatment": treatment, "vat_amount": _text(obj.vat_amount)})

    imported_fees = False
    for e in db.query(Expense).filter(Expense.tenant_id == tenant_id, Expense.expense_date >= start,
                                      Expense.expense_date <= end).order_by(Expense.expense_date):
        if e.external_ref:  # imported from eBay: covered by the figures from eBay's own invoice
            imported_fees = True
            continue
        item("expense", e, e.expense_date, " · ".join(p for p in (e.supplier, e.description) if p), e.amount)

    for p in db.query(Purchase).filter(Purchase.tenant_id == tenant_id, Purchase.cancelled_at.is_(None),
                                       Purchase.invoice_date >= start, Purchase.invoice_date <= end
                                       ).order_by(Purchase.invoice_date):
        item("purchase", p, p.invoice_date, " · ".join(x for x in (p.supplier, p.invoice_number) if x), p.total)

    if unclassified:
        open_points.append(f"{unclassified} Ausgabe(n) oder Einkaufsrechnung(en) sind noch nicht eingeordnet.")
    if without_vat and regular:
        open_points.append(f"Bei {without_vat} deutschen Rechnung(en) fehlt der Umsatzsteuerbetrag laut Rechnung.")

    figures = db.query(VatPeriodInput).filter(VatPeriodInput.tenant_id == tenant_id, VatPeriodInput.year == year,
                                              VatPeriodInput.period == period).first()
    ebay_orders = db.query(Order.id).filter(
        Order.tenant_id == tenant_id, Order.ebay_account_id.isnot(None),
        func.coalesce(Order.ordered_at, Order.created_at) >= start,
        func.coalesce(Order.ordered_at, Order.created_at) < date.fromordinal(end.toordinal() + 1)).first()
    has_ebay = imported_fees or ebay_orders is not None

    treatment = figures.ebay_fees_tax if figures else None
    if treatment == "de_vat":
        if figures.ebay_amount is None:
            open_points.append("Die Umsatzsteuer laut eBay-Gebührenrechnung fehlt.")
        else:
            input_vat += figures.ebay_amount
    elif treatment == "eu_service":
        if figures.ebay_amount is None:
            open_points.append("Der Nettobetrag laut eBay-Gebührenrechnung (§ 13b) fehlt.")
        else:
            sums["eu_service"] += figures.ebay_amount
    elif treatment is None and has_ebay:
        open_points.append("eBay-Gebühren: Bitte anhand der eBay-Rechnung angeben, wie sie ausgestellt sind.")

    if regular:
        if figures and figures.labels_vat is not None:
            input_vat += figures.labels_vat
        elif has_ebay:
            open_points.append("Die Umsatzsteuer auf Versandetiketten laut Rechnung fehlt (0, wenn keine ausgewiesen ist).")

    data = svc.sales(db, tenant_id, start, end)
    if regular:
        pending = [r for r in data["manual_refunds"] if r["refund_date"] is None]
        if pending:
            open_points.append(f"Bei {len(pending)} Erstattung(en) ist das Zahlungsdatum unbekannt. Bitte unten eintragen "
                               "oder den eBay-Abgleich erneut ausführen.")
        if data["mismatched"]:
            open_points.append("Bei diesen Bestellungen sind die datierten Erstattungen höher als der Betrag in der "
                               "Bestellung: " + ", ".join(data["mismatched"]))
        if data["foreign_orders"]:
            open_points.append(f"{data['foreign_orders']} Verkauf/Verkäufe gingen ins Ausland. Diese Hilfe rechnet nur "
                               "Inlandsumsätze zu 19 % richtig.")
        if data["orders_without_country"]:
            open_points.append(f"Bei {data['orders_without_country']} Verkauf/Verkäufen ist das Lieferland unbekannt.")

    rows = svc.compute(regular=regular, sales_gross=data["net_gross"], sums=sums, input_vat=input_vat,
                       advance_payment=advance_payment if regular else ZERO)

    notes = []
    if regular:
        notes.append("Gilt nur, wenn alle Verkäufe dieses Zeitraums hier erfasst sind und zu 19 % in Deutschland "
                     "steuerpflichtig sind. Umsätze zu 7 % oder 0 % und steuerfreie Umsätze werden nicht erkannt.")
        notes.append("Verkäufe zählen am Bestelldatum. Wurde erst im nächsten Zeitraum geliefert, bitte prüfen.")
    else:
        notes.append("Kleinunternehmer (§ 19 UStG): kein Vorsteuerabzug. Eine Voranmeldung kommt nur in "
                     "besonderen Fällen in Betracht, z. B. bei Steuer nach § 13b. Eigene Umsätze (Kz 48) "
                     "werden hier nicht ausgefüllt. Bitte mit Finanzamt oder Steuerberatung klären.")
    if sums.get("eu_goods") and not regular:
        notes.append("Warenkäufe aus der EU: Für Kleinunternehmer gelten eigene Voraussetzungen "
                     "(u. a. Erwerbsschwelle). Bitte prüfen, ob Kz 89 anfällt.")
    if any(i["tax_treatment"] == "third_country" for i in items):
        notes.append("Warenkäufe aus Drittländern: Eine gezahlte Einfuhrumsatzsteuer bitte als eigene Ausgabe "
                     "mit 'Einfuhrumsatzsteuer' erfassen.")

    return {
        "year": year, "period": period, "start": start.isoformat(), "end": end.isoformat(),
        "vat_mode": "regular" if regular else "kleinunternehmer",
        "complete": not open_points, "open_points": open_points, "notes": notes, "rows": rows, "items": items,
        "sales": {"sold": str(data["sold"]), "refunded": str(data["refunded"]), "net": str(data["net_gross"])},
        "manual_refunds": data["manual_refunds"] if regular else [],
        "ebay": {"ebay_fees_tax": treatment, "ebay_amount": _text(figures.ebay_amount if figures else None),
                 "labels_vat": _text(figures.labels_vat if figures else None)},
    }


class ClassifyIn(BaseModel):
    kind: Literal["expense", "purchase"]
    id: uuid.UUID
    tax_treatment: str | None = None
    vat_amount: Decimal | None = Field(None, ge=0, le=1000000, decimal_places=2)


@router.put("/ustva/classification")
def classify(payload: ClassifyIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    model, allowed = ((Expense, svc.EXPENSE_TREATMENTS) if payload.kind == "expense"
                      else (Purchase, svc.PURCHASE_TREATMENTS))
    if payload.tax_treatment is not None and payload.tax_treatment not in allowed:
        raise HTTPException(status_code=422, detail="Diese Einordnung gibt es hier nicht.")
    obj = db.query(model).filter(model.id == payload.id, model.tenant_id == current_user.tenant_id).first()
    if not obj:
        raise HTTPException(status_code=404, detail="Eintrag nicht gefunden")
    vat = payload.vat_amount if payload.tax_treatment == "de_vat" else None
    gross = obj.amount if payload.kind == "expense" else obj.total
    if vat is not None and vat > svc.vat_inside(gross) + Decimal("0.05"):
        raise HTTPException(status_code=422,
                            detail=f"Bei {gross} € brutto können höchstens {svc.vat_inside(gross)} € Umsatzsteuer enthalten sein.")
    obj.tax_treatment = payload.tax_treatment
    obj.vat_amount = vat
    db.commit()
    return {"tax_treatment": obj.tax_treatment, "vat_amount": _text(obj.vat_amount)}


class PeriodInputsIn(BaseModel):
    period: str
    year: int = svc.FORM_YEAR
    ebay_fees_tax: Literal["de_vat", "eu_service", "no_vat"] | None = None
    ebay_amount: Decimal | None = Field(None, ge=0, le=1000000, decimal_places=2)
    labels_vat: Decimal | None = Field(None, ge=0, le=1000000, decimal_places=2)


@router.put("/ustva/period-inputs")
def period_inputs(payload: PeriodInputsIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _check_period(payload.year, payload.period)
    figures = db.query(VatPeriodInput).filter(
        VatPeriodInput.tenant_id == current_user.tenant_id, VatPeriodInput.year == payload.year,
        VatPeriodInput.period == payload.period).first()
    if figures is None:
        figures = VatPeriodInput(tenant_id=current_user.tenant_id, year=payload.year, period=payload.period)
        db.add(figures)
    figures.ebay_fees_tax = payload.ebay_fees_tax
    figures.ebay_amount = payload.ebay_amount if payload.ebay_fees_tax in ("de_vat", "eu_service") else None
    figures.labels_vat = payload.labels_vat
    db.commit()
    return {"ok": True}


class RefundDateIn(BaseModel):
    order_id: uuid.UUID
    refund_date: date | None = None


@router.put("/ustva/refund-date")
def refund_date(payload: RefundDateIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """The day a refund without an eBay transaction was paid (typed in by hand)."""
    order = db.query(Order).filter(Order.id == payload.order_id, Order.tenant_id == current_user.tenant_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Bestellung nicht gefunden")
    if payload.refund_date and payload.refund_date < svc._day(order.ordered_at or order.created_at):
        raise HTTPException(status_code=422, detail="Die Erstattung kann nicht vor dem Verkauf liegen.")
    order.refund_date = payload.refund_date
    db.commit()
    return {"refund_date": _text(order.refund_date)}
