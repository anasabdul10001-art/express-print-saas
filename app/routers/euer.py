"""The cash-basis report (EÜR) as figures and as a list of every booking."""

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.core.rate_limit import limiter
from app.models.tenant import Tenant
from app.models.user import User
from app.services import euer_service, export_service

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
    return euer_service.report(db, current_user.tenant_id, start, end, group_by,
                               _vat_mode(db, current_user.tenant_id))


def _vat_mode(db: Session, tenant_id) -> str:
    return db.query(Tenant.vat_mode).filter(Tenant.id == tenant_id).scalar()


@router.get("/bookings.csv")
def euer_bookings(
    start: date,
    end: date,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _check(start, end)
    rows, _ = euer_service.entries(db, current_user.tenant_id, start, end)
    return Response(
        content=export_service.bookings_csv(rows), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="euer-buchungen-{start}-{end}.csv"'})


@router.get("/export.zip")
@limiter.limit("4/minute")
def euer_export(
    request: Request,
    start: date,
    end: date,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """All bookings with their documents and the issued invoices, as one ZIP."""
    _check(start, end)
    try:
        archive = export_service.build(db, current_user.tenant_id, start, end,
                                       _vat_mode(db, current_user.tenant_id))
    except export_service.TooLarge:
        raise HTTPException(status_code=400, detail="Zu viele Belege für eine Datei. Bitte einen kürzeren "
                                                    "Zeitraum wählen.")

    def chunks():
        with archive:
            while block := archive.read(256 * 1024):
                yield block

    return StreamingResponse(chunks(), media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="buchhaltung-{start}-{end}.zip"'})
