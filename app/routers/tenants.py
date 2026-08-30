from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.tenant import PrintModeUpdate, TenantOut

router = APIRouter(prefix="/tenants", tags=["tenants"])


@router.get("/me", response_model=TenantOut)
def get_my_tenant(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.query(Tenant).filter(Tenant.id == current_user.tenant_id).first()


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
