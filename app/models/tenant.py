import uuid

from sqlalchemy import Column, String, DateTime, CheckConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_name = Column(String(255), nullable=False)
    country_code = Column(String(2), nullable=False, default="DE")
    status = Column(String(20), nullable=False, default="active")

    # Controls how shipping labels get printed for this tenant's orders:
    #   INSTANT           - auto-print the moment an order arrives
    #   DASHBOARD_MANUAL   - order sits in a list; user clicks "Print" per order
    #   INTEGRATED          - one PDF combining the shipping label + a picking
    #                          slip (product photo, name, shelf location)
    print_mode = Column(String(20), nullable=False, default="DASHBOARD_MANUAL")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "print_mode IN ('INSTANT','DASHBOARD_MANUAL','INTEGRATED')",
            name="ck_tenants_print_mode",
        ),
    )

