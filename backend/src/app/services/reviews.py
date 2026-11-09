"""Live access and version checks for file reviews.

Never cache a viewer's space role here: team removal, grant revocation, a
trashed node or a deleted space must take effect on the next request.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FileVersion, Node, SpaceGrant, Team, User
from app.models.review import ReviewApprovalEvent, ReviewComment, ReviewMention
from app.schemas.reviews import (
    AccessGrantOut,
    AccessReasonOut,
    ApprovalDecisionOut,
    CommentOut,
    MentionedPersonOut,
    NodeAccessOut,
    ReviewOut,
)
from app.services import access


def live_node(db: Session, user: User, node_id: UUID, needed: str = "viewer") -> tuple[Node, str]:
    """404 for hidden/trash, including a child under a trashed ancestor."""
    return access.get_live_node_for(db, user, node_id, needed)


def file_version(db: Session, node: Node, number: int | None) -> FileVersion:
    if node.kind != "file":
        raise HTTPException(status.HTTP_409_CONFLICT, "Reviews are for files")
    if number is None:
        version = node.current_version
    else:
        version = db.scalar(
            select(FileVersion).where(FileVersion.node_id == node.id, FileVersion.number == number)
        )
    if version is None or version.node_id != node.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Version not found")
    return version


def explain_access(db: Session, user: User, node: Node, role: str) -> NodeAccessOut:
    """Only this person's actual sources of access, not other team members.

    Managers additionally see the explicit share list, as they can already
    edit it. A non-manager never receives someone else's grants.
    """
    space = access.get_space(db, node.space_id)
    reasons: list[AccessReasonOut] = []
    if user.is_admin:
        reasons.append(
            AccessReasonOut(source="admin", role="manager", label="Xource administrator")
        )
    direct = db.scalar(
        select(SpaceGrant).where(
            SpaceGrant.space_id == space.id,
            SpaceGrant.principal_type == "user",
            SpaceGrant.principal_id == user.id,
        )
    )
    if direct:
        reasons.append(AccessReasonOut(source="direct", role=direct.role, label="Shared with you"))
    team_ids = access.team_ids_for(db, user)
    if team_ids:
        team_grants = db.execute(
            select(SpaceGrant.role, Team.name)
            .join(Team, Team.id == SpaceGrant.principal_id)
            .where(
                SpaceGrant.space_id == space.id,
                SpaceGrant.principal_type == "team",
                SpaceGrant.principal_id.in_(team_ids),
                Team.deleted_at.is_(None),
            )
            .order_by(Team.name)
        ).all()
        reasons.extend(
            AccessReasonOut(source="team", role=grant_role, label=name)
            for grant_role, name in team_grants
        )
    if space.visibility == "organization":
        reasons.append(
            AccessReasonOut(source="organization", role="viewer", label="Everyone with organization access")
        )
    grants: list[AccessGrantOut] | None = None
    if role == "manager":
        grants = []
        for grant in db.scalars(
            select(SpaceGrant)
            .where(SpaceGrant.space_id == space.id)
            .order_by(SpaceGrant.created_at, SpaceGrant.id)
        ):
            if grant.principal_type == "user":
                principal = db.get(User, grant.principal_id)
                label = (principal.name or principal.email) if principal else "Former user"
            else:
                principal = db.get(Team, grant.principal_id)
                label = principal.name if principal else "Former team"
                if principal and principal.deleted_at is not None:
                    label += " (deleted)"
            grants.append(
                AccessGrantOut(
                    principal_type=grant.principal_type,
                    principal_id=grant.principal_id,
                    role=grant.role,
                    label=label,
                )
            )
    return NodeAccessOut(
        node_id=node.id,
        space_id=space.id,
        role=role,
        reasons=reasons,
        grants=grants,
        confined=access.is_confined(space, role),
    )


def known_recipients(db: Session, node: Node, identifiers: list[str]) -> list[User]:
    """Resolve explicit IDs/emails, rejecting unknown or hidden people equally.

    Never create placeholder accounts or search the company directory here;
    membership/space access is checked live on the same transaction as POST.
    """
    space = access.get_space(db, node.space_id)
    people: dict[UUID, User] = {}
    for raw in identifiers:
        value = raw.strip()
        try:
            uid = UUID(value)
        except ValueError:
            uid = None
        person = (
            db.get(User, uid)
            if uid is not None
            else db.scalar(select(User).where(User.email == value.lower()))
        )
        if (
            person is None
            or access.space_role(db, person, space) is None
            or not access.space_scope(db, person, space).can_see(db, node)
        ):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "A mentioned person must be an existing user who can view this document",
            )
        people[person.id] = person
    return list(people.values())


def comments_out(db: Session, comments: list[ReviewComment]) -> list[CommentOut]:
    if not comments:
        return []
    mentions: dict[UUID, list[MentionedPersonOut]] = {c.id: [] for c in comments}
    for mention in db.scalars(
        select(ReviewMention)
        .where(ReviewMention.comment_id.in_([c.id for c in comments]))
        .order_by(ReviewMention.email)
    ):
        mentions[mention.comment_id].append(
            MentionedPersonOut(user_id=mention.user_id, email=mention.email)
        )
    resolvers = {
        p.id: p.email
        for p in db.scalars(
            select(User).where(
                User.id.in_([c.resolved_by_id for c in comments if c.resolved_by_id is not None])
            )
        )
    }
    return [
        CommentOut(
            id=c.id,
            node_id=c.node_id,
            version=c.version_number,
            body=c.body,
            author_id=c.author_id,
            author_label=c.author_label,
            created_at=c.created_at,
            resolved=c.resolved_at is not None,
            resolved_at=c.resolved_at,
            resolved_by=resolvers.get(c.resolved_by_id),
            mentions=mentions[c.id],
        )
        for c in comments
    ]


def review_out(node: Node, version: FileVersion, events: list[ReviewApprovalEvent]) -> ReviewOut:
    latest = events[0] if events else None
    return ReviewOut(
        node_id=node.id,
        version=version.number,
        current_version=node.current_version.number,
        approved=bool(latest and latest.approved),
        status=("approved" if latest.approved else "not_approved") if latest else "pending",
        decisions=[
            ApprovalDecisionOut(
                id=e.id,
                version=e.version_number,
                approved=e.approved,
                actor_label=e.actor_label,
                created_at=e.created_at,
            )
            for e in events
        ],
    )
