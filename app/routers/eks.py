"""The helper figures for the Jobcenter's Anlage EKS."""

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.tenant import Tenant
from app.models.user import User
from app.services import eks_pdf_service, eks_profile_service, eks_service

router = APIRouter(prefix="/accounting/eks", tags=["accounting"])

UNREADABLE = ("Die gespeicherten Angaben können nicht gelesen werden. Bitte löschen und neu eingeben, "
              "oder das Formular ohne diese Angaben herunterladen.")


def _owner(user: User = Depends(get_current_user)) -> User:
    """The saved personal details are for the account owner only."""
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Nur der Kontoinhaber darf diese Angaben sehen.")
    return user


class ProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    consent: bool = False
    details: eks_profile_service.EksDetails


def _saved(db: Session, tenant_id):
    try:
        return eks_profile_service.load(db, tenant_id)
    except eks_profile_service.UnreadableProfile:
        raise HTTPException(status_code=409, detail=UNREADABLE)


@router.get("/profile")
def get_profile(response: Response, user: User = Depends(_owner), db: Session = Depends(get_db)):
    response.headers["Cache-Control"] = "no-store"
    details = _saved(db, user.tenant_id)
    return {"saved": details is not None,
            "details": (details or eks_profile_service.EksDetails()).model_dump()}


@router.put("/profile")
def put_profile(payload: ProfileIn, user: User = Depends(_owner), db: Session = Depends(get_db)):
    if not payload.consent:
        raise HTTPException(status_code=400, detail="Bitte zuerst dem Speichern zustimmen.")
    if payload.details.is_empty():
        raise HTTPException(status_code=400, detail="Es gibt nichts zu speichern. Zum Entfernen bitte „Löschen“.")
    eks_profile_service.save(db, user.tenant_id, payload.details)
    return {"saved": True}


@router.delete("/profile", status_code=204)
def delete_profile(user: User = Depends(_owner), db: Session = Depends(get_db)):
    eks_profile_service.delete(db, user.tenant_id)


def _report(db: Session, user: User, start: date, end: date) -> dict:
    if end < start:
        raise HTTPException(status_code=400, detail="Das Enddatum liegt vor dem Startdatum.")
    if not 2000 <= start.year <= 2100 or not 2000 <= end.year <= 2100 \
            or len(eks_service.months_between(start, end)) > eks_service.MAX_MONTHS:
        raise HTTPException(status_code=400, detail="Der Zeitraum darf höchstens 12 Kalendermonate umfassen.")
    if db.query(Tenant.vat_mode).filter(Tenant.id == user.tenant_id).scalar() == "regular":
        raise HTTPException(status_code=400, detail="Die EKS-Auswertung gibt es nur ohne Umsatzsteuerpflicht. "
                            "Mit Umsatzsteuer verlangt das Formular Nettobeträge und die echte Vor- und Umsatzsteuer.")
    return eks_service.report(db, user.tenant_id, start, end)


@router.get("")
def eks_report(
    start: date,
    end: date,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _report(db, current_user, start, end)


@router.get("/table.csv")
def eks_csv(
    start: date,
    end: date,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return Response(content=eks_service.to_csv(_report(db, current_user, start, end)),
                    media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="eks-{start}-{end}.csv"'})


@router.get("/form.pdf")
def eks_form(
    start: date,
    end: date,
    final: bool = False,
    sums: bool = True,
    personal: bool = True,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The official Anlage EKS with the figures written into it."""
    if start.day != 1 or (end + timedelta(days=1)).day != 1:
        raise HTTPException(status_code=400, detail="Für das Formular bitte ganze Kalendermonate wählen.")
    if final and end >= date.today():
        raise HTTPException(status_code=400, detail="Eine abschließende Erklärung geht erst, wenn der Zeitraum "
                                                    "vorbei ist.")
    data = _report(db, current_user, start, end)
    details = None
    if personal and current_user.role == "owner":
        saved = _saved(db, current_user.tenant_id)
        details = eks_profile_service.form_values(saved) if saved else None
    try:
        content, complete = eks_pdf_service.fill(data, final, sums, details)
    except eks_pdf_service.FormPeriodError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(content=content, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="Anlage-EKS-{start}-{end}.pdf"',
        "X-Remarks-Complete": "1" if complete else "0",
        "Cache-Control": "no-store",
        "Access-Control-Expose-Headers": "X-Remarks-Complete"})
