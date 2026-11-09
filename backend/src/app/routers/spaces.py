"""Spaces (shared drives), the personal drive, and sharing grants."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import Space, SpaceGrant, Team, TeamMember, User
from app.schemas.xource import GrantIn, GrantOut, SpaceIn, SpaceOut, SpacePatch
from app.services import access, audit, sharing, tree
from app.services.views import space_out, space_stats

router = APIRouter(prefix="/spaces", tags=["spaces"])


@router.get("", response_model=list[SpaceOut])
def list_spaces(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Every space you can see, with your role and size. Your drive comes first."""
    if not access.is_bot(user):
        tree.personal_space(db, user)
        db.commit()
    roles = access.roles_by_space(db, user)
    if not roles:
        return []
    spaces = db.scalars(select(Space).where(Space.id.in_(roles.keys()))).all()
    # Admins see every space, but other people's personal drives stay out of
    # their list (still reachable by id for support).
    spaces = [s for s in spaces if s.owner_user_id in (None, user.id)]
    # Sizes count only what you can see (your own folder in a personal-folders space).
    stats = space_stats(db, [s.id for s in spaces], access.visible_nodes(db, user, roles))
    mine = access.confinement(db, user, roles).roots
    spaces.sort(key=lambda s: (s.owner_user_id != user.id, s.name.lower()))
    return [
        space_out(s, roles[s.id], stats=stats.get(s.id, (0, 0)), my_folder_id=mine.get(s.id))
        for s in spaces
    ]


@router.get("/drive", response_model=SpaceOut)
def my_drive(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Your personal space ("My Drive"), created on first use."""
    if access.is_bot(user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bots have no personal drive")
    space = tree.personal_space(db, user)
    db.commit()
    stats = space_stats(db, [space.id]).get(space.id, (0, 0))
    return space_out(space, "manager", stats=stats)


@router.post("", response_model=SpaceOut, status_code=status.HTTP_201_CREATED)
def create_space(
    body: SpaceIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if access.is_bot(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Bots have read-only access")
    slug = tree.unique_slug(db, Space, body.slug or body.name, 80)
    space = Space(
        slug=slug,
        name=body.name.strip(),
        description=body.description,
        visibility=body.visibility,
        created_by_id=user.id,
    )
    db.add(space)
    db.flush()
    db.add(
        SpaceGrant(
            space_id=space.id,
            principal_type="user",
            principal_id=user.id,
            role="manager",
            granted_by_id=user.id,
        )
    )
    audit.record(
        db,
        user,
        "space.create",
        f"Created space {space.name}",
        request=request,
        space_id=space.id,
        details={"visibility": space.visibility},
    )
    db.commit()
    db.refresh(space)
    return space_out(space, "manager", stats=(0, 0))


@router.get("/deleted", response_model=list[SpaceOut])
def deleted_spaces(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Deleted spaces you could restore, most recently deleted first."""
    roles = access.roles_by_space(db, user, deleted=True)
    if not roles:
        return []
    spaces = db.scalars(
        select(Space).where(Space.id.in_(roles.keys())).order_by(Space.deleted_at.desc())
    ).all()
    spaces = [s for s in spaces if access.can_delete_space(db, user, s)]
    stats = space_stats(db, [s.id for s in spaces])
    return [_full_out(db, s, roles[s.id], stats.get(s.id, (0, 0)), can_delete=True) for s in spaces]


def _full_out(
    db: Session,
    space: Space,
    role: str | None,
    stats: tuple[int, int],
    *,
    can_delete: bool,
    scope: access.Scope | None = None,
) -> SpaceOut:
    my_folder = scope.root.id if scope is not None and scope.root is not None else None
    out = space_out(space, role, stats=stats, my_folder_id=my_folder)
    out.can_delete = can_delete
    out.deleted_at = space.deleted_at
    if space.deleted_by_id:
        who = db.get(User, space.deleted_by_id)
        out.deleted_by = (who.name or who.email) if who else None
    return out


@router.get("/{space_id}", response_model=SpaceOut)
def get_space(
    space_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    space = access.get_space(db, space_id)
    scope = access.space_scope(db, user, space, "viewer", create=True)
    if scope.created:
        db.commit()
    stats = space_stats(db, [space.id], scope.node_clause()).get(space.id, (0, 0))
    return _full_out(
        db,
        space,
        scope.role,
        stats,
        can_delete=access.can_delete_space(db, user, space),
        scope=scope,
    )


@router.delete("/{space_id}", response_model=SpaceOut)
def delete_space(
    space_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete a space. It disappears for everyone, but nothing is destroyed:
    files, history and sharing come back with ``/restore``."""
    space = access.get_space(db, space_id)
    role = access.require_space_role(db, user, space, "manager")
    if space.owner_user_id is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A personal drive cannot be deleted")
    if not access.can_delete_space(db, user, space):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Deleting a space needs manager access of your own, or owner or maintainer of a "
            "managing team",
        )
    files, size = space_stats(db, [space.id]).get(space.id, (0, 0))
    space.deleted_at = tree.now()
    space.deleted_by_id = user.id
    audit.record(
        db,
        user,
        "space.delete",
        f"Deleted space {space.name}",
        request=request,
        space_id=space.id,
        details={"files": files, "bytes": size},
    )
    db.commit()
    db.refresh(space)
    return _full_out(db, space, role, (files, size), can_delete=True)


@router.post("/{space_id}/restore", response_model=SpaceOut)
def restore_space(
    space_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Bring a deleted space back exactly as it was."""
    space = access.get_space(db, space_id, include_deleted=True)
    role = access.space_role(db, user, space)
    if role is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Space not found")
    if not access.can_delete_space(db, user, space):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You cannot restore this space")
    if space.deleted_at is not None:
        deleted_at = space.deleted_at
        space.deleted_at = None
        space.deleted_by_id = None
        space.updated_at = tree.now()
        audit.record(
            db,
            user,
            "space.restore",
            f"Restored space {space.name}",
            request=request,
            space_id=space.id,
            details={"deleted_at": deleted_at.isoformat()},
        )
        db.commit()
        db.refresh(space)
    stats = space_stats(db, [space.id]).get(space.id, (0, 0))
    return _full_out(db, space, role, stats, can_delete=True)


@router.patch("/{space_id}", response_model=SpaceOut)
def update_space(
    space_id: UUID,
    body: SpacePatch,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Serialize settings changes with concurrent writes.
    space, role = access.lock_space_for(db, user, space_id, "manager")
    changes = body.model_dump(exclude_unset=True)
    if space.owner_user_id is not None and changes.get("visibility") == "organization":
        raise HTTPException(422, "A personal drive cannot be company-wide")
    if space.owner_user_id is not None and changes.get("personal_folders"):
        raise HTTPException(422, "A personal drive cannot have personal folders")
    if changes.get("personal_folders") is None:
        changes.pop("personal_folders", None)
    before = {k: getattr(space, k) for k in changes}
    for key, value in changes.items():
        setattr(space, key, value)
    space.updated_at = tree.now()
    audit.record(
        db,
        user,
        "space.update",
        f"Updated settings of {space.name}",
        request=request,
        space_id=space.id,
        details={"before": before, "after": changes},
    )
    db.commit()
    db.refresh(space)
    return space_out(space, role)


# ---------------------------------------------------------------- sharing


def _grant_out(db: Session, grant: SpaceGrant) -> GrantOut:
    if grant.principal_type == "user":
        person = db.get(User, grant.principal_id)
        label = (person.name or person.email) if person else "Unknown user"
        detail = person.email if person else None
    else:
        team = db.get(Team, grant.principal_id)
        label = team.name if team else "Unknown team"
        if team is not None and team.deleted_at is not None:
            label += " (deleted team)"
        count = db.scalar(
            select(func.count())
            .select_from(TeamMember)
            .where(TeamMember.team_id == grant.principal_id)
        )
        detail = f"Team · {count} member{'s' if count != 1 else ''}" if team else None
    return GrantOut(
        id=grant.id,
        principal_type=grant.principal_type,
        principal_id=grant.principal_id,
        role=grant.role,
        label=label,
        detail=detail,
        created_at=grant.created_at,
    )


@router.get("/{space_id}/grants", response_model=list[GrantOut])
def list_grants(
    space_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Full private sharing policy is manager-only; viewers use their access explanation."""
    access.require_space_role(db, user, space_id, "manager")
    grants = db.scalars(
        select(SpaceGrant).where(SpaceGrant.space_id == space_id).order_by(SpaceGrant.created_at)
    ).all()
    return [_grant_out(db, g) for g in grants]


@router.put("/{space_id}/grants", response_model=GrantOut)
def share(
    space_id: UUID,
    body: GrantIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Give a person or a team a role on the space (or change their role)."""
    space = access.get_space(db, space_id)
    access.require_space_role(db, user, space, "manager")
    if (body.email is None) == (body.team_id is None):
        raise HTTPException(422, "Send exactly one of email, team_id")
    if body.email is not None:
        person = tree.user_by_email(db, body.email)
        ptype, pid, label = "user", person.id, person.email
    else:
        team = db.get(Team, body.team_id)
        if team is None or team.deleted_at is not None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Team not found")
        ptype, pid, label = "team", team.id, f"team {team.name}"

    grant, _ = sharing.upsert_grant(
        db,
        user,
        space,
        principal_type=ptype,
        principal_id=pid,
        label=label,
        role=body.role,
        request=request,
    )
    db.commit()
    db.refresh(grant)
    return _grant_out(db, grant)


@router.delete("/{space_id}/grants/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
def unshare(
    space_id: UUID,
    grant_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    space = access.get_space(db, space_id)
    access.require_space_role(db, user, space, "manager")
    grant = db.get(SpaceGrant, grant_id)
    if grant is None or grant.space_id != space.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Grant not found")
    if grant.role == "manager" and sharing.manager_count(db, space.id) <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "A space needs at least one manager")
    out = _grant_out(db, grant)
    db.delete(grant)
    audit.record(
        db,
        user,
        "space.unshare",
        f"Removed {out.label}'s access to {space.name}",
        request=request,
        space_id=space.id,
        details={
            "principal_type": grant.principal_type,
            "principal_id": str(grant.principal_id),
            "role": grant.role,
        },
    )
    db.commit()
