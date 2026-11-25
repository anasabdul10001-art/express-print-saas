"""
The account holder's own data: how long buyer data is kept, and a download of
the records stored for the account (access and portability, Art. 15 and 20 GDPR).
"""

import io
import json
import zipfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import LargeBinary, select
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.core.rate_limit import limiter
from app.database import Base
from app.models.tenant import Tenant
from app.models.user import User
from app.services import eks_profile_service, retention_service

router = APIRouter(prefix="/account", tags=["account"])


def _owner(user: User = Depends(get_current_user)) -> User:
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Nur der Kontoinhaber darf das.")
    return user


class RetentionIn(BaseModel):
    months: int


@router.get("/retention")
def get_retention(user: User = Depends(_owner), db: Session = Depends(get_db)):
    months = db.query(Tenant.buyer_data_retention_months).filter(Tenant.id == user.tenant_id).scalar()
    return {"months": months, "choices": list(retention_service.CHOICES)}


@router.put("/retention")
def set_retention(payload: RetentionIn, user: User = Depends(_owner), db: Session = Depends(get_db)):
    if payload.months not in retention_service.CHOICES:
        raise HTTPException(status_code=400, detail="Bitte 6, 12, 24 oder 36 Monate wählen.")
    db.query(Tenant).filter(Tenant.id == user.tenant_id).update(
        {Tenant.buyer_data_retention_months: payload.months})
    db.commit()
    return {"months": payload.months}


# secrets are not "your data" to hand out, and a copy of them would be a risk
SECRET_MARKS = ("password", "_encrypted", "_hash", "signing_jwe", "token")


def _public_columns(table):
    return [c for c in table.columns
            if not isinstance(c.type, LargeBinary) and not any(mark in c.name for mark in SECRET_MARKS)]


def collect(db: Session, tenant_id) -> dict:
    """Every row that belongs to the account, per table. Tables without a tenant_id
    are included through their parent."""
    tables = Base.metadata.tables
    result: dict = {}

    def take(name, condition):
        columns = _public_columns(tables[name])
        rows = db.execute(select(*columns).where(condition)).mappings().all()
        result[name] = [dict(row) for row in rows]

    take("tenants", tables["tenants"].c.id == tenant_id)
    for name, table in sorted(tables.items()):
        # one-time sign-in states and reset tokens say nothing about the person
        if "tenant_id" in table.columns and name != "tenants" and not name.endswith(("_states", "_tokens")):
            take(name, table.c.tenant_id == tenant_id)
    for child, key, parent in (("order_items", "order_id", "orders"),
                               ("purchase_lines", "purchase_id", "purchases")):
        own = select(tables[parent].c.id).where(tables[parent].c.tenant_id == tenant_id)
        take(child, tables[child].c[key].in_(own))
    return result


@router.get("/export")
@limiter.limit("3/minute")
def export(request: Request, user: User = Depends(_owner), db: Session = Depends(get_db)):
    data = collect(db, user.tenant_id)
    try:
        details = eks_profile_service.load(db, user.tenant_id)
        data["eks_angaben"] = details.model_dump() if details else None
    except eks_profile_service.UnreadableProfile:
        data["eks_angaben"] = "nicht lesbar"
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    note = ("Deine in ShipSync gespeicherten Daten (konto.json), Stand "
            f"{datetime.now(timezone.utc):%d.%m.%Y %H:%M} UTC.\r\n"
            "Nicht enthalten: Passwörter, Zugangsschlüssel und Dateien.\r\n"
            "Rechnungen, Lieferantenrechnungen und Belege lädst du unter Buchhaltung > EÜR > "
            "„Alles mit Belegen (ZIP)“ je Zeitraum herunter.\r\n")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("konto.json", text)
        archive.writestr("LIESMICH.txt", "﻿" + note)
    return Response(content=buffer.getvalue(), media_type="application/zip", headers={
        "Content-Disposition": 'attachment; filename="shipsync-meine-daten.zip"',
        "Cache-Control": "no-store"})
