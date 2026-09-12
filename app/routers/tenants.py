from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.plan import Plan
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.tenant import PrintModeUpdate, TenantOut, TenantUpdate

router = APIRouter(prefix="/tenants", tags=["tenants"])


def _with_plan_name(db: Session, tenant: Tenant) -> TenantOut:
    plan = db.query(Plan).filter(Plan.id == tenant.plan_id).first() if tenant.plan_id else None
    out = TenantOut.model_validate(tenant)
    out.plan_name = plan.name if plan else None
    return out


@router.get("/me", response_model=TenantOut)
def get_my_tenant(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tenant = db.query(Tenant).filter(Tenant.id == current_user.tenant_id).first()
    return _with_plan_name(db, tenant)


@router.patch("/me", response_model=TenantOut)
def update_my_tenant(
    payload: TenantUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant = db.query(Tenant).filter(Tenant.id == current_user.tenant_id).first()
    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(tenant, field, value)
    db.commit()
    db.refresh(tenant)
    return tenant


@router.patch("/me/print-mode", response_model=TenantOut)
def update_print_mode(
    payload: PrintModeUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant = db.query(Tenant).filter(Tenant.id == current_user.tenant_id).first()
    tenant.print_mode = payload.print_mode
    db.commit()
    db.refresh(tenant)
    return tenant
