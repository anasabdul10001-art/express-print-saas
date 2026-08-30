import uuid

from sqlalchemy import Column, String, DateTime, SmallInteger, Text, ForeignKey, CheckConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.database import Base


class PrintJob(Base):
    """
    order_id is now a real foreign key to orders.id (Order module now
    exists). Still nullable - this keeps the manual /print-jobs/test
    endpoint working for pure connectivity tests that aren't tied to a
    real order, while every job created via the new
    POST /print-jobs/order/{order_id} endpoint will always set it.
    """

    __tablename__ = "print_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    print_agent_id = Column(UUID(as_uuid=True), ForeignKey("print_agents.id", ondelete="CASCADE"), nullable=False)
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=True)

    label_pdf_url = Column(Text, nullable=False)
    rotation_degrees = Column(SmallInteger, nullable=False, default=0)
    label_format = Column(String(20), nullable=False, default="4x6")

    status = Column(String(20), nullable=False, default="pending")
    error_message = Column(Text)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    printed_at = Column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("rotation_degrees IN (0,90,180,270)", name="ck_print_jobs_rotation"),
        CheckConstraint("status IN ('pending','processing','printed','failed')", name="ck_print_jobs_status"),
    )
