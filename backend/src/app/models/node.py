"""The file tree, its immutable version history, and content-addressed blobs.

The design borrows git's core idea:

- **Blobs are content-addressed.** A blob's primary key is the SHA-256 of its
  bytes and it is stored once in object storage (SeaweedFS) at
  ``blobs/sha256/<aa>/<bb>/<sha>``. Uploading identical bytes twice — the
  same drawing in two folders, or re-uploading an unchanged file — stores
  them once.
- **Versions are append-only commits.** Every upload to a file adds a
  ``FileVersion`` row with the next number, the blob it points at, the author,
  and an optional message. Versions are never edited or deleted; "restore
  version 3" creates version N+1 pointing at version 3's blob, exactly like a
  revert commit.
- **Nodes are the working tree.** A node is a folder or a file at a place in a
  space. Renames, moves and deletes change the node and are written to the
  audit log; they never touch history. Delete is a soft delete (trash).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db import Base


class Blob(Base):
    __tablename__ = "blobs"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default="application/octet-stream"
    )
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Node(Base):
    __tablename__ = "nodes"
    __table_args__ = (CheckConstraint("kind in ('file', 'folder')", name="ck_nodes_kind"),)
    # Sibling-name uniqueness (case-insensitive, live nodes only, root
    # included via NULLS NOT DISTINCT) is a partial unique index created in the
    # 0002 migration — SQLAlchemy cannot express it portably here.

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    # RESTRICT (0011): spaces are soft-deleted; a hard delete must not take
    # the tree and its history with it.
    space_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="RESTRICT"), nullable=False
    )
    parent_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    current_version_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("file_versions.id", ondelete="SET NULL", use_alter=True),
    )
    created_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Trash: a delete stamps the node and every live descendant with the same
    # timestamp, so restore can bring back exactly that batch.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    current_version: Mapped[FileVersion | None] = relationship(
        foreign_keys=[current_version_id], lazy="joined", post_update=True
    )


class FileVersion(Base):
    """One commit in a file's history.

    Append-only at the database level (0011): a trigger rejects UPDATE, and
    DELETE unless the transaction set ``xit.purge = on`` (only
    ``services.tree.purge``) and the owning node is in the trash.
    """

    __tablename__ = "file_versions"
    __table_args__ = (UniqueConstraint("node_id", "number", name="uq_file_versions_node_number"),)

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    node_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    blob_sha256: Mapped[str] = mapped_column(
        String(64), ForeignKey("blobs.sha256", ondelete="RESTRICT"), nullable=False
    )
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    # Set when this version was produced by restoring an earlier one.
    restored_from: Mapped[int | None] = mapped_column(Integer)
    # RESTRICT: history rows are never updated, not even to forget an author.
    author_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    author_label: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
