"""Requests for access to a space, and the per-recipient notification outbox.

An ``AccessRequest`` with ``space_id`` set is actionable: the space's managers
see it and can approve (which upserts an ordinary ``SpaceGrant``) or deny it.
A request whose target does not exist (or is otherwise not actionable, such as
a second request in a space that already has one pending from the same
person) is stored as an inert *receipt* with ``space_id`` NULL, so that the
requester's own list looks the same either way. Receipts are never shown to
anyone else and can only be cancelled.

``Notification`` rows are written in the same transaction as the change that
caused them. A background loop hands undelivered rows to the configured
channels (``services.notifications``); with none configured they are only
shown in-app.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base

REQUEST_ROLES = ("viewer", "editor")
REQUEST_STATUSES = ("pending", "approved", "denied", "cancelled")


class AccessRequest(Base):
    __tablename__ = "access_requests"
    __table_args__ = (
        CheckConstraint("requested_role in ('viewer', 'editor')", name="ck_access_requests_role"),
        CheckConstraint(
            "status in ('pending', 'approved', 'denied', 'cancelled')",
            name="ck_access_requests_status",
        ),
        CheckConstraint(
            "decided_role is null or decided_role in ('viewer', 'editor')",
            name="ck_access_requests_decided_role",
        ),
        CheckConstraint("target_type in ('space', 'node')", name="ck_access_requests_target"),
        # One actionable pending request per person per space.
        Index(
            "uq_access_requests_pending_space",
            "space_id",
            "requester_id",
            unique=True,
            postgresql_where=text("status = 'pending' and space_id is not null"),
        ),
        # One pending receipt per person per thing they asked for.
        Index(
            "uq_access_requests_pending_target",
            "requester_id",
            "target_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # NULL = inert receipt (see module docstring); never shown to managers.
    space_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="SET NULL")
    )
    # What the requester asked for, exactly as sent (no FK: it may not exist).
    target_type: Mapped[str] = mapped_column(String(8), nullable=False)
    target_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    requester_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    requested_role: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    decided_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_role: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


NOTIFICATION_KINDS = ("access.requested", "access.decided", "space.shared", "review.mention")


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_recipient_created", "recipient_user_id", "created_at"),
        Index(
            "ix_notifications_undelivered",
            "next_attempt_at",
            postgresql_where=text("delivered_at is null and next_attempt_at is not null"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    recipient_user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Optional external delivery state; no channel is bundled.
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    channel: Mapped[str | None] = mapped_column(String(32))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    # NULL = no further delivery attempts (delivered, or given up).
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
