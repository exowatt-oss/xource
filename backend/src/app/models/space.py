"""Spaces — shared drives — and who can reach them.

A space is the unit of sharing, like a shared drive: it holds a tree of folders and files, and access is granted on
the space as a whole.

Space roles (ascending): ``viewer`` < ``editor`` < ``manager``.

- ``viewer``  — browse, preview, download, read history.
- ``editor``  — upload, create folders, rename, move, trash, restore versions.
- ``manager`` — everything an editor can do, plus edit settings and sharing.

A space with ``personal_folders`` confines everyone who is not a space manager
or a Xource admin to one top-level folder named after their email (``alice/``);
they cannot see anything else in the space. ``access.Scope`` enforces it.

Deleting a space is soft: ``deleted_at`` hides it (and everything in it) from
every endpoint, and it can be restored as it was, files, history and sharing
included. Who may delete is decided by ``access.can_delete_space``.

A grant gives a role to one principal: a ``user`` or a ``team``. A space with
``visibility = 'organization'`` additionally gives every signed-in user
``viewer``.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base

SPACE_ROLES = ("viewer", "editor", "manager")
VISIBILITIES = ("restricted", "organization")
PRINCIPAL_TYPES = ("user", "team")


class Space(Base):
    __tablename__ = "spaces"
    __table_args__ = (
        CheckConstraint(
            "visibility in ('restricted', 'organization')", name="ck_spaces_visibility"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, server_default="restricted")
    # Non-managers see only their own top-level folder (see access.Scope).
    personal_folders: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Set for a user's personal "My Drive" space; NULL for shared spaces.
    # RESTRICT: deleting a users row must never cascade into their drive's
    # history (0011). Spaces are soft-deleted; users are never deleted.
    owner_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), unique=True
    )
    created_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SpaceGrant(Base):
    __tablename__ = "space_grants"
    __table_args__ = (
        UniqueConstraint(
            "space_id", "principal_type", "principal_id", name="uq_space_grants_principal"
        ),
        CheckConstraint("principal_type in ('user', 'team')", name="ck_space_grants_type"),
        CheckConstraint("role in ('viewer', 'editor', 'manager')", name="ck_space_grants_role"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    space_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False
    )
    # Polymorphic principal: a users.id or a teams.id, chosen by principal_type.
    principal_type: Mapped[str] = mapped_column(String(8), nullable=False)
    principal_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    granted_by_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
