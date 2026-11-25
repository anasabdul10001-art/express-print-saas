"""The cash-basis report (EÜR) as figures and as a list of every booking."""

import csv
import io
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.tenant import Tenant
from app.models.user import User
from app.services import euer_service

router = APIRouter(prefix="/accounting/euer", tags=["accounting"])

MAX_DAYS = 3660


def _check(start: date, end: date) -> None:
    if end < start:
        raise HTTPException(status_code=400, detail="Das Enddatum liegt vor dem Startdatum.")
    if (end - start).days > MAX_DAYS or not 2000 <= start.year <= 2100:
        raise HTTPException(status_code=400, detail="Zeitraum ungültig (maximal 10 Jahre).")


@router.get("")
def euer_report(
    start: date,
    end: date,
    group_by: Literal["month", "quarter", "year"] = Query("quarter"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _check(start, end)
    mode = db.query(Tenant.vat_mode).filter(Tenant.id == current_user.tenant_id).scalar()
    return euer_service.report(db, current_user.tenant_id, start, end, group_by, mode)


def _safe(text: str) -> str:
    """A spreadsheet must show typed text, never run it as a formula."""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@router.get("/bookings.csv")
def euer_bookings(
    start: date,
    end: date,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _check(start, end)
    rows, _ = euer_service.entries(db, current_user.tenant_id, start, end)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(["Zahlungsdatum", "Art", "Position", "Betrag (€)", "Beleg / Bezug"])
    for row in rows:
        income = row["kind"] in euer_service.INCOME
        writer.writerow([row["day"].strftime("%d.%m.%Y"), "Einnahme" if income else "Ausgabe",
                         euer_service.LABELS[row["kind"]], f"{row['amount']:.2f}".replace(".", ","),
                         _safe(row["text"])])
    return Response(
        content="﻿" + buffer.getvalue(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="euer-buchungen-{start}-{end}.csv"'})
