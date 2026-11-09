"""Shared helpers to turn ORM rows into response schemas."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models import FileVersion, Node, Space
from app.schemas.xource import Crumb, NodeOut, SpaceOut, VersionOut


def version_out(v: FileVersion | None) -> VersionOut | None:
    return VersionOut.model_validate(v) if v is not None else None


def node_out(node: Node, version_count: int | None = None) -> NodeOut:
    return NodeOut(
        id=node.id,
        space_id=node.space_id,
        parent_id=node.parent_id,
        kind=node.kind,
        name=node.name,
        created_at=node.created_at,
        updated_at=node.updated_at,
        deleted_at=node.deleted_at,
        version=version_out(node.current_version),
        version_count=version_count,
    )


def version_counts(db: Session, node_ids: list[UUID]) -> dict[UUID, int]:
    if not node_ids:
        return {}
    rows = db.execute(
        select(FileVersion.node_id, func.count())
        .where(FileVersion.node_id.in_(node_ids))
        .group_by(FileVersion.node_id)
    )
    return dict(rows.all())


def space_out(
    space: Space,
    role: str | None,
    *,
    stats: tuple[int, int] | None = None,
    my_folder_id: UUID | None = None,
) -> SpaceOut:
    return SpaceOut(
        id=space.id,
        slug=space.slug,
        name=space.name,
        description=space.description,
        visibility=space.visibility,
        personal_folders=bool(space.personal_folders),
        my_folder_id=my_folder_id,
        is_personal=space.owner_user_id is not None,
        created_at=space.created_at,
        updated_at=space.updated_at,
        my_role=role,
        file_count=stats[0] if stats else None,
        total_bytes=stats[1] if stats else None,
    )


def space_stats(
    db: Session, space_ids: list[UUID], visible: ColumnElement[bool] | None = None
) -> dict[UUID, tuple[int, int]]:
    """(live files, bytes) per space; ``visible`` (from ``access``) limits
    what is counted to what the caller can see."""
    if not space_ids:
        return {}
    stmt = (
        select(Node.space_id, func.count(Node.id), func.coalesce(func.sum(FileVersion.size), 0))
        .join(FileVersion, FileVersion.id == Node.current_version_id)
        .where(Node.space_id.in_(space_ids), Node.kind == "file", Node.deleted_at.is_(None))
        .group_by(Node.space_id)
    )
    if visible is not None:
        stmt = stmt.where(visible)
    rows = db.execute(stmt)
    return {sid: (int(n), int(b)) for sid, n, b in rows}


def crumbs(space: Space, chain: list[Node]) -> list[Crumb]:
    return [Crumb(id=None, name=space.name)] + [Crumb(id=n.id, name=n.name) for n in chain]
