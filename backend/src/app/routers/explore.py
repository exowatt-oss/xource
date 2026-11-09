"""Cross-space views: search, recent files, activity log, people, and stats."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import Date, and_, case, cast, false, func, literal, or_, select
from sqlalchemy import text as sql_text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import AuditEvent, Blob, FileVersion, Node, Space, Team, TeamMember, User
from app.models.document import DocumentIndex
from app.schemas.xource import ActivityOut, NodeOut, PersonOut, SearchHit, StatsOut
from app.services import access, tree
from app.services.views import node_out, version_counts

router = APIRouter(tags=["explore"])


def _visible(db: Session, user: User, space_id: UUID | None):
    """Spaces you can see (or the one asked for) and the matching ``Node``
    filter, which keeps personal-folders members inside their own folder."""
    roles = access.visible_roles(db, user, space_id)
    return list(roles), access.visible_nodes(db, user, roles)


@router.get("/search", response_model=list[SearchHit])
def search(
    response: Response,
    q: str = Query(min_length=1, max_length=200),
    space_id: UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Name or indexed-current-content search, scoped *before* text matching.

    Historical versions, hidden personal folders, Trash, deleted spaces and
    revoked grants cannot contribute a hit or snippet. Cache records are never
    queried in isolation from the live current node/version pair.
    """
    response.headers["Cache-Control"] = "no-store"
    ids, visible = _visible(db, user, space_id)
    if not ids:
        return []
    q = q.strip()
    if not q:
        raise HTTPException(422, "Enter a search term")
    pattern = "%" + q.lower().replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_") + "%"
    name_match = func.lower(Node.name).like(pattern, escape="\\")
    content_search = len(q) >= 3
    content_match = (
        func.lower(DocumentIndex.text).like(pattern, escape="\\") if content_search else false()
    )
    # Return a small window, never 200 full 250k-character caches into API RAM.
    start = func.greatest(func.strpos(func.lower(DocumentIndex.text), q.lower()) - 65, 1)
    window = func.concat(
        case((start > 1, "…"), else_=""),
        func.substr(DocumentIndex.text, start, len(q) + 155),
        case((func.length(DocumentIndex.text) >= start + len(q) + 155, "…"), else_=""),
    )
    excerpt = case((name_match, literal(None)), else_=window) if content_search else literal(None)
    db.execute(sql_text("SET LOCAL statement_timeout = '2500ms'"))
    stmt = select(Node, excerpt)
    if content_search:
        stmt = stmt.outerjoin(
            DocumentIndex,
            and_(
                DocumentIndex.node_id == Node.id,
                DocumentIndex.version_id == Node.current_version_id,
                DocumentIndex.status == "ready",
            ),
        )
    stmt = (
        stmt.where(
            Node.space_id.in_(ids),
            visible,
            access.untrashed_nodes(ids),
            Node.deleted_at.is_(None),
            or_(name_match, content_match),
        )
        .order_by(Node.updated_at.desc())
        .limit(min(limit, 50) if content_search else limit)
    )
    try:
        rows = db.execute(stmt).unique().all()
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != "57014":
            raise
        db.rollback()
        raise HTTPException(503, "Search is busy; try a narrower term or space") from exc
    spaces = {
        s.id: s
        for s in db.scalars(
            select(Space).where(
                Space.id.in_({n.space_id for n, _ in rows}), Space.deleted_at.is_(None)
            )
        )
    }
    out = []
    for n, indexed_text in rows:
        # roles_by_space checked live access above; this redundant guard also
        # protects against a space being deleted after the initial role read.
        if n.space_id not in spaces:
            continue
        chain = tree.path_of(db, n)
        in_name = q.casefold() in n.name.casefold()
        snippet = None if in_name else _snippet(indexed_text or "", q)
        out.append(
            SearchHit(
                **node_out(n).model_dump(),
                space_name=spaces[n.space_id].name,
                path_text="/".join(c.name for c in chain[:-1]),
                match_type="filename" if in_name else "content",
                snippet=snippet,
            )
        )
    return out


def _snippet(text: str, query: str) -> str | None:
    """A short window around a match, only after the caller's access filter."""
    offset = text.casefold().find(query.casefold())
    if offset < 0:
        return None
    start = max(0, offset - 65)
    end = min(len(text), offset + len(query) + 90)
    return (
        ("…" if start else "")
        + " ".join(text[start:end].split())
        + ("…" if end < len(text) else "")
    )


@router.get("/recent", response_model=list[NodeOut])
def recent(
    limit: int = Query(30, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Files most recently changed in spaces you can see."""
    ids, visible = _visible(db, user, None)
    if not ids:
        return []
    rows = (
        db.scalars(
            select(Node)
            .where(
                visible, access.untrashed_nodes(ids), Node.kind == "file", Node.deleted_at.is_(None)
            )
            .order_by(Node.updated_at.desc())
            .limit(limit)
        )
        .unique()
        .all()
    )
    counts = version_counts(db, [n.id for n in rows])
    return [node_out(n, counts.get(n.id)) for n in rows]


@router.get("/activity", response_model=list[ActivityOut])
def activity(
    space_id: UUID | None = None,
    node_id: UUID | None = None,
    team_id: UUID | None = None,
    before_id: int | None = None,
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The audit log, newest first. Scope it with ``space_id``, ``node_id`` or
    ``team_id``; page with ``before_id``. You only see events for spaces you
    can view, teams you belong to, and your own actions."""
    stmt = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
    if before_id is not None:
        stmt = stmt.where(AuditEvent.id < before_id)
    split = access.Confinement()
    if node_id is not None:
        node, _ = access.get_node_for(db, user, node_id, "viewer")
        stmt = stmt.where(AuditEvent.node_id == node.id)
    elif space_id is not None:
        # History outlives the space: a deleted space's log stays readable.
        space = access.get_space(db, space_id, include_deleted=True)
        role = access.space_role(db, user, space)
        if role is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Space not found")
        roles = {space.id: role}
        split = access.confinement(db, user, roles)
        stmt = stmt.where(access.visible_events(db, user, roles, split))
    elif team_id is not None:
        if not user.is_admin and access.team_role(db, user, db.get(Team, team_id)) is None:
            return []
        stmt = stmt.where(AuditEvent.team_id == team_id)
    elif not user.is_admin:
        # Deleted spaces stay in the feed for those who could see them, so a
        # deletion is never silent.
        roles = access.roles_by_space(db, user)
        roles.update(access.roles_by_space(db, user, deleted=True))
        split = access.confinement(db, user, roles)
        teams = access.team_ids_for(db, user) + _deleted_team_ids(db, user)
        clauses = [access.own_events(user, split)]
        if roles:
            clauses.append(access.visible_events(db, user, roles, split))
        if teams:
            clauses.append(AuditEvent.team_id.in_(teams))
        stmt = stmt.where(or_(*clauses))
    if not user.is_admin:
        policy_roles = access.roles_by_space(db, user)
        policy_roles.update(access.roles_by_space(db, user, deleted=True))
        managers = [sid for sid, role in policy_roles.items() if role == "manager"]
        # Sharing identities are manager-only, including the audit-log side door.
        # Access requests too: a requester's own access.* events would otherwise
        # reveal whether the item they asked about exists.
        stmt = stmt.where(
            or_(
                AuditEvent.action.not_in(
                    (
                        "space.share",
                        "space.unshare",
                        "access.request",
                        "access.approve",
                        "access.deny",
                        "access.cancel",
                    )
                ),
                AuditEvent.space_id.in_(managers),
            )
        )
    out = []
    for e in db.scalars(stmt):
        item = ActivityOut.model_validate(e)
        if access.hide_event_details(e, split):
            item.details = {}
        out.append(item)
    return out


def _deleted_team_ids(db: Session, user: User) -> list[UUID]:
    return list(
        db.scalars(
            select(TeamMember.team_id)
            .join(Team, Team.id == TeamMember.team_id)
            .where(TeamMember.user_id == user.id, Team.deleted_at.is_not(None))
        )
    )


@router.get("/users", response_model=list[PersonOut])
def people(
    q: str = Query("", max_length=200),
    limit: int = Query(20, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Company directory (people who have used Xource or been shared with)."""
    stmt = select(User).order_by(func.lower(func.coalesce(User.name, User.email))).limit(limit)
    if q:
        pattern = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(User.email).like(pattern),
                func.lower(func.coalesce(User.name, "")).like(pattern),
            )
        )
    return [PersonOut.model_validate(u) for u in db.scalars(stmt)]


@router.get("/stats", response_model=StatsOut)
def stats(
    space_id: UUID | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Storage at a glance for the spaces you can see: counts, bytes (logical
    vs actually stored after de-duplication), file types, uploads per day."""
    ids, visible = _visible(db, user, space_id)
    ids = ids or [UUID(int=0)]
    live = (Node.space_id.in_(ids), visible, Node.deleted_at.is_(None))
    files = db.scalar(select(func.count()).where(*live, Node.kind == "file")) or 0
    folders = db.scalar(select(func.count()).where(*live, Node.kind == "folder")) or 0
    version_q = (
        select(FileVersion)
        .join(Node, Node.id == FileVersion.node_id)
        .where(Node.space_id.in_(ids), visible)
        .subquery()
    )
    versions = db.scalar(select(func.count()).select_from(version_q)) or 0
    logical = db.scalar(select(func.coalesce(func.sum(version_q.c.size), 0))) or 0
    stored = (
        db.scalar(
            select(func.coalesce(func.sum(Blob.size), 0)).where(
                Blob.sha256.in_(select(version_q.c.blob_sha256))
            )
        )
        or 0
    )
    by_type = [
        {"type": t, "files": int(n), "bytes": int(b)}
        for t, n, b in db.execute(
            select(FileVersion.content_type, func.count(), func.sum(FileVersion.size))
            .join(Node, Node.current_version_id == FileVersion.id)
            .where(*live)
            .group_by(FileVersion.content_type)
            .order_by(func.count().desc())
            .limit(12)
        )
    ]
    since = datetime.now(tree.UTC) - timedelta(days=30)
    day = cast(FileVersion.created_at, Date)
    uploads = [
        {"day": d.isoformat(), "versions": int(n), "bytes": int(b)}
        for d, n, b in db.execute(
            select(day, func.count(), func.sum(FileVersion.size))
            .join(Node, Node.id == FileVersion.node_id)
            .where(Node.space_id.in_(ids), visible, FileVersion.created_at >= since)
            .group_by(day)
            .order_by(day)
        )
    ]
    return StatsOut(
        spaces=len([i for i in ids if i.int != 0]),
        files=files,
        folders=folders,
        versions=versions,
        logical_bytes=int(logical),
        stored_bytes=int(stored),
        users=db.scalar(select(func.count()).select_from(User)) or 0,
        teams=db.scalar(select(func.count()).select_from(Team)) or 0,
        by_type=by_type,
        uploads_by_day=uploads,
    )
