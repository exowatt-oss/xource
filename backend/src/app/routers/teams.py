"""Teams: create, rename, delete and restore, and manage members and their roles."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import SpaceGrant, Team, TeamMember, User
from app.schemas.xource import (
    TeamDetailOut,
    TeamIn,
    TeamMemberIn,
    TeamMemberOut,
    TeamOut,
    TeamPatch,
)
from app.services import access, audit, tree
from app.services.views import space_out

router = APIRouter(prefix="/teams", tags=["teams"])


def _team_out(db: Session, team: Team, user: User, *, detail: bool = False):
    role = access.team_role(db, user, team)
    base = dict(
        id=team.id,
        slug=team.slug,
        name=team.name,
        description=team.description,
        created_at=team.created_at,
        source=team.source,
        synced_at=team.synced_at,
        member_count=len(team.members),
        my_role=role,
        deleted_at=team.deleted_at,
    )
    if team.deleted_by_id:
        who = db.get(User, team.deleted_by_id)
        base["deleted_by"] = (who.name or who.email) if who else None
    if not detail:
        return TeamOut(**base)
    order = {"owner": 0, "maintainer": 1, "member": 2}
    members = sorted(
        team.members, key=lambda m: (order[m.role], (m.user.name or m.user.email).lower())
    )
    return TeamDetailOut(**base, members=[TeamMemberOut.model_validate(m) for m in members])


def _get_team(db: Session, team_id: UUID, *, include_deleted: bool = False) -> Team:
    team = db.get(Team, team_id)
    if team is None or (team.deleted_at is not None and not include_deleted):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Team not found")
    return team


@router.get("", response_model=list[TeamOut])
def list_teams(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """All teams in the company (team names are not secret), yours first."""
    teams = db.scalars(
        select(Team).where(Team.deleted_at.is_(None)).order_by(func.lower(Team.name))
    ).all()
    out = [_team_out(db, t, user) for t in teams]
    out.sort(key=lambda t: t.my_role is None)
    return out


@router.get("/deleted", response_model=list[TeamOut])
def deleted_teams(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Deleted teams you could restore (you were an owner or maintainer)."""
    teams = db.scalars(
        select(Team).where(Team.deleted_at.is_not(None)).order_by(Team.deleted_at.desc())
    ).all()
    out = []
    for t in teams:
        role = access.team_role(db, user, t)
        if role is not None and access.TEAM_RANK[role] >= access.TEAM_RANK["maintainer"]:
            out.append(_team_out(db, t, user))
    return out


@router.post("", response_model=TeamDetailOut, status_code=status.HTTP_201_CREATED)
def create_team(
    body: TeamIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if access.is_bot(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Bots have read-only access")
    team = Team(
        slug=tree.unique_slug(db, Team, body.slug or body.name, 64),
        name=body.name.strip(),
        description=body.description,
        created_by_id=user.id,
    )
    db.add(team)
    db.flush()
    db.add(TeamMember(team_id=team.id, user_id=user.id, role="owner"))
    audit.record(
        db, user, "team.create", f"Created team {team.name}", request=request, team_id=team.id
    )
    db.commit()
    db.refresh(team)
    return _team_out(db, team, user, detail=True)


@router.get("/{team_id}", response_model=TeamDetailOut)
def get_team(team_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _team_out(db, _get_team(db, team_id), user, detail=True)


@router.get("/{team_id}/spaces")
def team_spaces(
    team_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Spaces this team has been given access to (that you can also see)."""
    team = _get_team(db, team_id)
    roles = access.roles_by_space(db, user)
    rows = (
        db.execute(
            select(SpaceGrant).where(
                SpaceGrant.principal_type == "team", SpaceGrant.principal_id == team.id
            )
        )
        .scalars()
        .all()
    )
    out = []
    for g in rows:
        if g.space_id in roles:
            space = access.get_space(db, g.space_id)
            out.append({"space": space_out(space, roles[g.space_id]), "team_role": g.role})
    return out


@router.patch("/{team_id}", response_model=TeamDetailOut)
def update_team(
    team_id: UUID,
    body: TeamPatch,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    team = _get_team(db, team_id)
    access.require_team_role(db, user, team, "owner")
    changes = body.model_dump(exclude_unset=True)
    for key, value in changes.items():
        setattr(team, key, value)
    audit.record(
        db,
        user,
        "team.update",
        f"Updated team {team.name}",
        request=request,
        team_id=team.id,
        details=changes,
    )
    db.commit()
    db.refresh(team)
    return _team_out(db, team, user, detail=True)


@router.delete("/{team_id}", response_model=TeamOut)
def delete_team(
    team_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete a team (owners and maintainers). Its members stop getting the
    team's space access, but members and grants are kept for ``/restore``."""
    team = _get_team(db, team_id)
    access.require_team_role(db, user, team, "maintainer")
    grants = db.scalar(
        select(func.count()).where(
            SpaceGrant.principal_type == "team", SpaceGrant.principal_id == team.id
        )
    )
    team.deleted_at = tree.now()
    team.deleted_by_id = user.id
    audit.record(
        db,
        user,
        "team.delete",
        f"Deleted team {team.name}",
        request=request,
        team_id=team.id,
        details={"members": len(team.members), "space_grants": grants or 0},
    )
    db.commit()
    db.refresh(team)
    return _team_out(db, team, user)


@router.post("/{team_id}/restore", response_model=TeamDetailOut)
def restore_team(
    team_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Bring a deleted team back with its members and space access."""
    team = _get_team(db, team_id, include_deleted=True)
    access.require_team_role(db, user, team, "maintainer")
    if team.deleted_at is not None:
        deleted_at = team.deleted_at
        team.deleted_at = None
        team.deleted_by_id = None
        audit.record(
            db,
            user,
            "team.restore",
            f"Restored team {team.name}",
            request=request,
            team_id=team.id,
            details={"deleted_at": deleted_at.isoformat()},
        )
        db.commit()
        db.refresh(team)
    return _team_out(db, team, user, detail=True)


def _owner_count(db: Session, team_id: UUID) -> int:
    return (
        db.scalar(
            select(func.count()).where(TeamMember.team_id == team_id, TeamMember.role == "owner")
        )
        or 0
    )


@router.put("/{team_id}/members", response_model=TeamDetailOut)
def add_member(
    team_id: UUID,
    body: TeamMemberIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Add someone (by email address) or change their role.

    Maintainers manage members; only owners can create or demote owners.
    """
    team = _get_team(db, team_id)
    my_role = access.require_team_role(db, user, team, "maintainer")
    person = tree.user_by_email(db, body.email)
    member = db.scalar(
        select(TeamMember).where(TeamMember.team_id == team.id, TeamMember.user_id == person.id)
    )
    previous = member.role if member else None
    if my_role != "owner" and "owner" in (body.role, previous):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only team owners can change owners")
    if previous == "owner" and body.role != "owner" and _owner_count(db, team.id) <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "A team needs at least one owner")
    if member is None:
        db.add(TeamMember(team_id=team.id, user_id=person.id, role=body.role))
        summary = f"Added {person.email} to {team.name} as {body.role}"
    else:
        member.role = body.role
        summary = f"Changed {person.email}'s role in {team.name} to {body.role}"
    audit.record(
        db,
        user,
        "team.member",
        summary,
        request=request,
        team_id=team.id,
        details={"user": person.email, "role": body.role, "previous": previous},
    )
    db.commit()
    db.refresh(team)
    return _team_out(db, team, user, detail=True)


@router.delete("/{team_id}/members/{user_id}", response_model=TeamDetailOut)
def remove_member(
    team_id: UUID,
    user_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    team = _get_team(db, team_id)
    member = db.scalar(
        select(TeamMember).where(TeamMember.team_id == team.id, TeamMember.user_id == user_id)
    )
    if member is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not a member")
    if user_id != user.id:  # anyone may leave; removing others needs rights
        my_role = access.require_team_role(db, user, team, "maintainer")
        if member.role == "owner" and my_role != "owner":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only team owners can remove owners")
    if member.role == "owner" and _owner_count(db, team.id) <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "A team needs at least one owner")
    email = member.user.email
    db.delete(member)
    audit.record(
        db,
        user,
        "team.member_remove",
        f"Removed {email} from {team.name}",
        request=request,
        team_id=team.id,
        details={"user": email},
    )
    db.commit()
    db.refresh(team)
    return _team_out(db, team, user, detail=True)
