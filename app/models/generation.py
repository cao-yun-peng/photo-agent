"""Generation：一次照片改造记录."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Generation(Base):
    __tablename__ = "generations"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "idempotency_key", name="uq_generations_user_idempotency"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_photo_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("photos.id", ondelete="SET NULL"),
        nullable=True,
    )
    skill_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("skills.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    extra_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_oss_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # pending / processing / done / failed
    status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False, index=True
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(
        String(32), default="wanx2.1-imageedit", nullable=False
    )
    cost_yuan: Mapped[Decimal] = mapped_column(Numeric(8, 4), default=Decimal("0"))
    estimated_cost_yuan: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), default=Decimal("0")
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    confirmation_token: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, unique=True
    )
    confirmation_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    quota_reserved: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )
    quota_reserved_day: Mapped[date | None] = mapped_column(Date, nullable=True)
    enqueue_status: Mapped[str] = mapped_column(
        String(16), default="not_queued", nullable=False, server_default="not_queued"
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, server_default="0"
    )
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
