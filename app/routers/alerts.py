from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.user import User
from app.services import alerts_service

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("")
def list_alerts(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return alerts_service.build_alerts(db, current_user.tenant_id)
