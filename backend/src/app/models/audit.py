"""Append-only audit log.

Every state-changing action (and every download) writes one row. The table
deliberately has no foreign keys — the log must outlive the rows it talks
about — and the 0002 migration installs a trigger that rejects UPDATE and
DELETE, so the app cannot rewrite history even by accident.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Identity, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    actor_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    actor_label: Mapped[str] = mapped_column(String(320), nullable=False)
    # e.g. "file.upload", "file.version", "node.move", "space.share".
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    space_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), index=True)
    node_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), index=True)
    team_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), index=True)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    ip: Mapped[str | None] = mapped_column(String(64))
