"""Derived document text and preview data, keyed by immutable file version.

Unlike the append-only file_versions table this cache may be replaced after a
failed extraction or when an editor explicitly retries. Never read it without
first checking access to the live node and binding its version to that node.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base


class DocumentIndex(Base):
    __tablename__ = "document_indexes"
    __table_args__ = (
        CheckConstraint(
            "status in ('ready', 'skipped', 'failed')", name="ck_document_indexes_status"
        ),
    )

    version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("file_versions.id", ondelete="CASCADE"), primary_key=True
    )
    node_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # Only safe, short, public diagnostic labels (never the parser's stderr).
    reason: Mapped[str | None] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    preview: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sa_text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class DocumentThumbnail(Base):
    """Replaceable PNG derivative; never a raw original or an extraction marker."""

    __tablename__ = "document_thumbnails"
    __table_args__ = (
        CheckConstraint("octet_length(png) <= 2097152", name="ck_document_thumbnails_size"),
    )

    version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("file_versions.id", ondelete="CASCADE"), primary_key=True
    )
    node_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    png: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DocumentProcessingAttempt(Base):
    """Small negative/backoff marker; no original bytes, text or parser diagnostics."""

    __tablename__ = "document_processing_attempts"
    __table_args__ = (
        CheckConstraint(
            "operation in ('extract', 'thumbnail')", name="ck_document_attempt_operation"
        ),
        CheckConstraint("attempts between 1 and 16", name="ck_document_attempt_count"),
        CheckConstraint("parser_failures between 0 and 3", name="ck_document_parser_failures"),
    )
    version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("file_versions.id", ondelete="CASCADE"), primary_key=True
    )
    operation: Mapped[str] = mapped_column(String(16), primary_key=True)
    node_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("nodes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    parser_failures: Mapped[int] = mapped_column(Integer, nullable=False)
    failed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
