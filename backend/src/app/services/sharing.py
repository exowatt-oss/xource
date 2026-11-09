"""Space grants: the one place a person or team is given a role on a space.

Used by ``PUT /api/spaces/{id}/grants`` and by access-request approval (web
or a future adapter), so the "keep at least one manager" rule, the audit entry and the
``space.shared`` notification are identical whichever way access is given.
The caller has already authorized the actor and commits the transaction.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Space, SpaceGrant, User
from app.services import audit, notifications
from app.services.access import RANK


def manager_count(db: Session, space_id: UUID) -> int:
    return (
        db.scalar(
            select(func.count()).where(
                SpaceGrant.space_id == space_id, SpaceGrant.role == "manager"
            )
        )
        or 0
    )


def upsert_grant(
    db: Session,
    actor: User,
    space: Space,
    *,
    principal_type: str,
    principal_id: UUID,
    label: str,
    role: str,
    request: Request | None = None,
    only_upgrade: bool = False,
    notify: bool = True,
    audit_details: dict | None = None,
) -> tuple[SpaceGrant, str | None]:
    """Create or change a grant, audit it, and notify a newly shared person.

    ``only_upgrade`` never lowers an existing direct grant (an approval for
    viewer must not demote someone a manager has since made editor). Returns
    the grant and its previous role. Flushes; never commits.
    """
    grant = db.scalar(
        select(SpaceGrant).where(
            SpaceGrant.space_id == space.id,
            SpaceGrant.principal_type == principal_type,
            SpaceGrant.principal_id == principal_id,
        )
    )
    previous = grant.role if grant else None
    if grant is not None and only_upgrade and RANK[grant.role] >= RANK[role]:
        return grant, previous
    if grant is None:
        grant = SpaceGrant(
            space_id=space.id,
            principal_type=principal_type,
            principal_id=principal_id,
            role=role,
            granted_by_id=actor.id,
        )
        db.add(grant)
    else:
        if previous == "manager" and role != "manager" and manager_count(db, space.id) <= 1:
            raise HTTPException(status.HTTP_409_CONFLICT, "A space needs at least one manager")
        grant.role = role
    db.flush()
    audit.record(
        db,
        actor,
        "space.share",
        f"Gave {label} {role} access to {space.name}"
        + (f" (was {previous})" if previous and previous != role else ""),
        request=request,
        space_id=space.id,
        details={
            "principal_type": principal_type,
            "principal_id": str(principal_id),
            "role": role,
            "previous": previous,
            **(audit_details or {}),
        },
    )
    # Individual people hear about a new or changed role. Team grants are not
    # fanned out to every member (large teams should not trigger an unbounded fanout).
    if notify and principal_type == "user" and principal_id != actor.id and previous != role:
        notifications.notify(
            db,
            principal_id,
            "space.shared",
            {
                "space_id": str(space.id),
                "space_name": space.name,
                "role": role,
                "previous": previous,
                "actor": audit.actor_label(actor),
                "personal_folders": bool(space.personal_folders) and role != "manager",
            },
        )
    return grant, previous
