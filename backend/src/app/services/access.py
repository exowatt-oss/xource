"""Access control: who may do what in which space.

Effective role in a space is the highest of:

- ``manager`` if the user is a Xource admin;
- the role of a grant to the user directly;
- the role of a grant to any team the user belongs to;
- ``viewer`` if the space's visibility is ``organization``.

Every router goes through :func:`require_space_role`, so there is exactly one
place where permissions are decided.

**Personal folders.** In a space with ``personal_folders`` on, everyone who is
not a space manager (Xource admins are managers everywhere) is a *member*
confined to one top-level folder named after their email's local part
(``alice@example.org`` -> ``alice/``). :class:`Scope` is the one place that
rule lives: every route that reaches a node goes through :func:`space_scope`,
:func:`node_scope` / :func:`get_node_for`, or the SQL filters
:func:`visible_nodes` / :func:`visible_events`. Outside their folder, a member
gets 404 — the same answer as for something that does not exist.

Deleted spaces and teams are soft-deleted. A deleted space reads as 404
everywhere, and a deleted team grants nothing, until someone restores it.
Deleting needs more than everyday management, because it takes the space away
from everyone (:func:`can_delete_space`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import and_, false, or_, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models import AuditEvent, Node, Space, SpaceGrant, Team, TeamMember, User
from app.services import audit, tree

RANK = {"viewer": 1, "editor": 2, "manager": 3}
TEAM_RANK = {"member": 1, "maintainer": 2, "owner": 3}


def is_bot(user: User) -> bool:
    """Verified machine callers carry a signed actor; human users do not."""
    return user.actor is not None


def team_ids_for(db: Session, user: User) -> list[UUID]:
    """Live teams the user belongs to (a deleted team grants nothing)."""
    return list(
        db.scalars(
            select(TeamMember.team_id)
            .join(Team, Team.id == TeamMember.team_id)
            .where(TeamMember.user_id == user.id, Team.deleted_at.is_(None))
        )
    )


def _principal_filter(db: Session, user: User):
    team_ids = team_ids_for(db, user)
    clauses = [(SpaceGrant.principal_type == "user") & (SpaceGrant.principal_id == user.id)]
    if team_ids:
        clauses.append(
            (SpaceGrant.principal_type == "team") & (SpaceGrant.principal_id.in_(team_ids))
        )
    return or_(*clauses)


def space_role(db: Session, user: User, space: Space) -> str | None:
    if user.is_admin:
        return "manager"
    best = 0
    roles = db.scalars(
        select(SpaceGrant.role).where(SpaceGrant.space_id == space.id, _principal_filter(db, user))
    )
    for role in roles:
        best = max(best, RANK[role])
    if space.visibility == "organization":
        best = max(best, RANK["viewer"])
    for name, rank in RANK.items():
        if rank == best:
            return name
    return None


def roles_by_space(db: Session, user: User, *, deleted: bool = False) -> dict[UUID, str]:
    """Effective role for every live space the user can see — one pass, no N+1.

    ``deleted=True`` answers the same question for deleted spaces instead (what
    the user could see, were it restored)."""
    result: dict[UUID, int] = {}
    alive = Space.deleted_at.is_not(None) if deleted else Space.deleted_at.is_(None)
    if user.is_admin:
        return {sid: "manager" for sid in db.scalars(select(Space.id).where(alive))}
    for sid in db.scalars(select(Space.id).where(Space.visibility == "organization", alive)):
        result[sid] = RANK["viewer"]
    for sid, role in db.execute(
        select(SpaceGrant.space_id, SpaceGrant.role)
        .join(Space, Space.id == SpaceGrant.space_id)
        .where(_principal_filter(db, user), alive)
    ):
        result[sid] = max(result.get(sid, 0), RANK[role])
    names = {rank: name for name, rank in RANK.items()}
    return {sid: names[rank] for sid, rank in result.items()}


def get_space(db: Session, space_id: UUID, *, include_deleted: bool = False) -> Space:
    space = db.get(Space, space_id)
    if space is None or (space.deleted_at is not None and not include_deleted):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Space not found")
    return space


def require_space_role(db: Session, user: User, space: Space | UUID, needed: str) -> str:
    """Return the user's role, or raise. Hidden spaces read as 404, not 403."""
    if not isinstance(space, Space):
        space = get_space(db, space)
    role = space_role(db, user, space) if space.deleted_at is None else None
    if role is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Space not found")
    if RANK[role] < RANK[needed]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"This needs {needed} access to the space")
    if needed != "viewer" and is_bot(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Bots have read-only access")
    return role


def lock_space_for(db: Session, user: User, space_id: UUID, needed: str) -> tuple[Space, str]:
    """Serialize a policy-sensitive write without queued DB/thread starvation."""
    require_space_role(db, user, space_id, needed)
    try:
        space = db.scalar(
            select(Space)
            .where(Space.id == space_id, Space.deleted_at.is_(None))
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True)
        )
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != "55P03":
            raise
        db.rollback()
        raise HTTPException(503, "This space is busy; please retry shortly") from exc
    if space is None:
        raise HTTPException(404, "Space not found")
    return space, require_space_role(db, user, space, needed)


def get_node_for(db: Session, user: User, node_id: UUID, needed: str) -> tuple[Node, str]:
    """The node and your role, or 404 if you can't see it (see :class:`Scope`)."""
    node, scope = node_scope(db, user, node_id, needed)
    return node, scope.role


def get_live_node_for(
    db: Session, user: User, node_id: UUID, needed: str = "viewer"
) -> tuple[Node, str]:
    """Authorize first, then reject Trash, trashed ancestors and corrupt cycles."""
    node, role = get_node_for(db, user, node_id, needed)
    seen: set[UUID] = set()
    ancestor: Node | None = node
    while ancestor is not None:
        if ancestor.id in seen or ancestor.deleted_at is not None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
        seen.add(ancestor.id)
        ancestor = db.get(Node, ancestor.parent_id) if ancestor.parent_id else None
    return node, role


# ---------------------------------------------------------------- personal folders


def is_confined(space: Space, role: str | None) -> bool:
    """Is someone with ``role`` limited to their own folder in ``space``?

    Managers (and Xource admins, whose role is always ``manager``) never are."""
    return bool(space.personal_folders) and role != "manager"


def personal_folder_name(user: User) -> str:
    """``alice@example.org`` -> ``alice``."""
    return user.email.split("@", 1)[0].strip().lower()


def repo_folder_ids(db: Session, space_ids: list[UUID]) -> set[UUID]:
    """No git-repository folders exist in the standalone storage core."""
    return set()


def _find_personal_folder(
    db: Session, user: User, space: Space, hidden: set[UUID]
) -> tuple[Node | None, Node | None]:
    """(their folder, or whatever else blocks its name) at the space root."""
    name = personal_folder_name(user)
    if not name:
        return None, None
    found = tree.find_child(db, space.id, None, name)
    if found is None:
        return None, None
    if found.kind != "folder" or found.id in hidden:
        return None, found
    return found, None


@dataclass
class Scope:
    """What one person can reach in one space: the whole space, or (a
    confined member of a personal-folders space) only their own folder."""

    space: Space
    role: str
    confined: bool = False
    # A confined member's own top-level folder; None until it exists.
    root: Node | None = None
    # Reserved for optional adapters which add hidden folder classes.
    hidden: set[UUID] = field(default_factory=set)
    created: bool = False  # the personal folder was made on this request

    def can_see(self, db: Session, node: Node) -> bool:
        if node.space_id != self.space.id:
            return False
        if not self.confined:
            return True
        if self.root is None:
            return False
        chain: list[Node] = []
        seen: set[UUID] = set()
        cur: Node | None = node
        while cur is not None and cur.id not in seen:
            seen.add(cur.id)
            chain.append(cur)
            cur = db.get(Node, cur.parent_id) if cur.parent_id else None
        top = chain[-1]
        return top.id == self.root.id and not any(n.id in self.hidden for n in chain)

    def require_visible(self, db: Session, node: Node | None, what: str = "Item") -> Node:
        if node is None or not self.can_see(db, node):
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found")
        return node

    def write_target(self, db: Session, folder_id: UUID | None) -> Node | None:
        """The folder a write lands in (None = the space root), checked.

        A confined member may only write inside their own folder."""
        folder = db.get(Node, folder_id) if folder_id is not None else None
        if folder_id is not None and (
            folder is None
            or folder.space_id != self.space.id
            or folder.kind != "folder"
            or folder.deleted_at is not None
            or not self.can_see(db, folder)
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Folder not found")
        if folder is None and self.confined:
            raise HTTPException(status.HTTP_403_FORBIDDEN, self.only_own_folder())
        return folder

    def upload_parent(self, db: Session, parent_id: UUID | None) -> UUID | None:
        """Where an upload's relative names resolve from. A confined member
        with no ``parent_id`` uploads into their own folder."""
        if self.confined and parent_id is None:
            if self.root is None:  # bots: nothing of theirs to write into
                raise HTTPException(status.HTTP_403_FORBIDDEN, self.only_own_folder())
            return self.root.id
        folder = self.write_target(db, parent_id)
        return folder.id if folder is not None else None

    def upload_parts(self, asked_parent_id: UUID | None, parts: list[str]) -> list[str]:
        """Drop a redundant leading ``<own folder>/`` from a member's upload
        path when they gave no ``parent_id``, so ``alice/a.txt`` and
        ``a.txt`` land in the same place."""
        if (
            self.confined
            and self.root is not None
            and asked_parent_id is None
            and len(parts) > 1
            and parts[0].lower() == self.root.name.lower()
        ):
            return parts[1:]
        return parts

    def only_own_folder(self) -> str:
        where = f" ({self.root.name}/)" if self.root is not None else ""
        return f"In this space you can only add things inside your own folder{where}"

    def require_not_root(self, node: Node) -> None:
        """Members cannot rename, move or trash their own folder itself."""
        if self.confined and self.root is not None and node.id == self.root.id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Your personal folder can't be renamed, moved or deleted; ask a space manager",
            )

    def require_whole_space(self, what: str) -> None:
        """For things that are not inside anyone's folder (linked repos, runs)."""
        if self.confined:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found")

    def node_clause(self) -> ColumnElement[bool]:
        """SQL filter on ``Node``: what this scope can see in its space."""
        if not self.confined:
            return Node.space_id == self.space.id
        if self.root is None:
            return false()
        return Node.id.in_(_subtree([self.root.id], self.hidden))


def _subtree(root_ids: list[UUID], hidden: set[UUID]):
    """Ids of ``root_ids`` and every descendant (trashed included), not
    descending into ``hidden`` folders."""
    tree_cte = select(Node.id).where(Node.id.in_(root_ids)).cte("scope_tree", recursive=True)
    child = select(Node.id).where(Node.parent_id == tree_cte.c.id)
    if hidden:
        child = child.where(Node.id.not_in(hidden))
    tree_cte = tree_cte.union_all(child)
    return select(tree_cte.c.id)


def _ensure_personal_folder(db: Session, user: User, scope: Scope) -> None:
    """Create a confined member's folder (created_by = them), race-safe."""
    name = personal_folder_name(user)
    try:
        with db.begin_nested():
            folder, created = tree.make_folder(db, user, scope.space.id, None, name)
    except IntegrityError:  # someone else's request made it first
        folder, created = tree.find_child(db, scope.space.id, None, name), False
    if folder is None or folder.kind != "folder" or folder.id in scope.hidden:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Your personal folder name {name!r} is taken in this space; ask a space manager",
        )
    scope.root, scope.created = folder, created
    if created:
        audit.record(
            db,
            user,
            "folder.create",
            f"Created personal folder {folder.name}",
            space_id=scope.space.id,
            node_id=folder.id,
            details={"personal_folder": True},
        )


def space_scope(
    db: Session, user: User, space: Space | UUID, needed: str = "viewer", *, create: bool = False
) -> Scope:
    """Your role in ``space`` (raising like :func:`require_space_role`) and
    what part of it you can reach.

    ``create=True`` makes a confined person's folder if it is missing; the
    caller commits. Bots never get a folder made, and see nothing in a
    personal-folders space (we cannot tell whose folder would be theirs)."""
    if not isinstance(space, Space):
        space = get_space(db, space)
    role = require_space_role(db, user, space, needed)
    scope = Scope(space=space, role=role, confined=is_confined(space, role))
    if not scope.confined or is_bot(user):
        return scope
    scope.hidden = repo_folder_ids(db, [space.id])
    scope.root, blocker = _find_personal_folder(db, user, space, scope.hidden)
    if scope.root is None and create and blocker is None:
        _ensure_personal_folder(db, user, scope)
    elif scope.root is None and create:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Your personal folder name {personal_folder_name(user)!r} is taken in this "
            "space; ask a space manager",
        )
    return scope


def node_scope(db: Session, user: User, node_id: UUID, needed: str) -> tuple[Node, Scope]:
    """A node you can see, and your scope in its space. Hidden nodes are 404."""
    node = db.get(Node, node_id)
    if node is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found")
    space = get_space(db, node.space_id)
    # Check visibility before rank, so a hidden node is 404 even for viewers
    # asking to write.
    scope = space_scope(db, user, space, "viewer")
    scope.require_visible(db, node)
    if RANK[scope.role] < RANK[needed]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"This needs {needed} access to the space")
    if needed != "viewer" and is_bot(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Bots have read-only access")
    return node, scope


@dataclass
class Confinement:
    """How a person sees a set of spaces (see :func:`confinement`)."""

    free: list[UUID] = field(default_factory=list)  # spaces seen whole
    confined: list[UUID] = field(default_factory=list)  # spaces where they are a member
    roots: dict[UUID, UUID] = field(default_factory=dict)  # space -> their own folder
    hidden: set[UUID] = field(default_factory=set)  # repo folders in confined spaces


def confinement(db: Session, user: User, roles: dict[UUID, str]) -> Confinement:
    """Split ``roles`` (space -> role) into whole spaces and personal-folder
    spaces where ``user`` is confined, with their folder in each."""
    pf = (
        {
            s.id: s
            for s in db.scalars(
                select(Space).where(Space.id.in_(list(roles)), Space.personal_folders.is_(True))
            )
        }
        if roles
        else {}
    )
    confined = [sid for sid, s in pf.items() if is_confined(s, roles[sid])]
    out = Confinement(free=[sid for sid in roles if sid not in confined], confined=confined)
    if confined and not is_bot(user):
        out.hidden = repo_folder_ids(db, confined)
        for sid in confined:
            root, _ = _find_personal_folder(db, user, pf[sid], out.hidden)
            if root is not None:
                out.roots[sid] = root.id
    return out


def visible_roles(db: Session, user: User, space_id: UUID | None = None) -> dict[UUID, str]:
    """Live spaces you can see with your role — or just ``space_id`` (404 if hidden)."""
    if space_id is not None:
        return {space_id: require_space_role(db, user, space_id, "viewer")}
    return roles_by_space(db, user)


def visible_nodes(db: Session, user: User, roles: dict[UUID, str]) -> ColumnElement[bool]:
    """SQL filter on ``Node`` for cross-space reads (search, recent, graph,
    stats…): whole spaces where you are not confined, only your own folder
    where you are. ``roles`` comes from :func:`visible_roles`."""
    split = confinement(db, user, roles)
    clauses: list[ColumnElement[bool]] = []
    if split.free:
        clauses.append(Node.space_id.in_(split.free))
    if split.roots:
        clauses.append(Node.id.in_(_subtree(list(split.roots.values()), split.hidden)))
    return or_(*clauses) if clauses else false()


def untrashed_nodes(space_ids: list[UUID]) -> ColumnElement[bool]:
    """Exclude Trash and its descendants before loading document content.

    A concurrent upload can leave a live child beneath a trashed parent. The
    ordinary per-node flag is therefore insufficient for search/inbox reads.
    UNION (not ALL) also terminates safely for a corrupt cyclic tree.
    This is not an access predicate: compose it with visible_nodes.
    """
    dead = (
        select(Node.id)
        .where(Node.space_id.in_(space_ids), Node.deleted_at.is_not(None))
        .cte("trashed_scope", recursive=True)
    )
    dead = dead.union(select(Node.id).where(Node.parent_id == dead.c.id))
    return Node.id.not_in(select(dead.c.id))


# Space-level events a confined member still sees, so a deletion is never
# silent — with their details (whole-space file counts, sizes) withheld.
_MEMBER_SPACE_EVENTS = ("space.delete", "space.restore")


def visible_events(
    db: Session, user: User, roles: dict[UUID, str], split: Confinement | None = None
) -> ColumnElement[bool]:
    """SQL filter on ``AuditEvent`` for the spaces in ``roles``: everything in
    a space you see whole; in a personal-folders space, only events about
    things in your own folder, and the space being deleted or restored."""
    if user.is_admin:
        return AuditEvent.space_id.in_(list(roles)) if roles else false()
    split = split if split is not None else confinement(db, user, roles)
    clauses: list[ColumnElement[bool]] = []
    if split.free:
        clauses.append(AuditEvent.space_id.in_(split.free))
    if split.confined:
        mine = [AuditEvent.action.in_(_MEMBER_SPACE_EVENTS)]
        if split.roots:
            roots = list(split.roots.values())
            mine.append(AuditEvent.node_id.in_(_subtree(roots, split.hidden)))
        clauses.append(and_(AuditEvent.space_id.in_(split.confined), or_(*mine)))
    return or_(*clauses) if clauses else false()


def own_events(user: User, split: Confinement) -> ColumnElement[bool]:
    """Your own actions — except in spaces where you are now confined, whose
    events are filtered by :func:`visible_events` (an action you took before
    the space went personal-folders may name someone else's folder)."""
    clause = AuditEvent.actor_id == user.id
    if split.confined:
        clause = and_(
            clause,
            or_(AuditEvent.space_id.is_(None), AuditEvent.space_id.not_in(split.confined)),
        )
    return clause


def hide_event_details(event: AuditEvent, split: Confinement) -> bool:
    """Should a confined member see this event without its details?"""
    return event.space_id in split.confined and event.action in _MEMBER_SPACE_EVENTS


def require_admin(user: User) -> None:
    """Xource admins only (never bots), for irreversible operations."""
    if not user.is_admin or is_bot(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only Xource admins can do this")


def can_delete_space(db: Session, user: User, space: Space) -> bool:
    """May this person delete (and restore) the space?

    Admins can. Otherwise you need manager access that is really yours: a
    manager grant to you directly, or a manager grant to a team you are an
    owner or maintainer of. Plain members of a managing team can run the
    space day to day but cannot take it away from everyone. Personal drives
    are never deleted, and bots are read-only.
    """
    if is_bot(user) or space.owner_user_id is not None:
        return False
    if user.is_admin:
        return True
    grants = db.execute(
        select(SpaceGrant.principal_type, SpaceGrant.principal_id).where(
            SpaceGrant.space_id == space.id, SpaceGrant.role == "manager"
        )
    ).all()
    team_ids = [pid for ptype, pid in grants if ptype == "team"]
    if any(ptype == "user" and pid == user.id for ptype, pid in grants):
        return True
    if not team_ids:
        return False
    lead = db.scalar(
        select(TeamMember.id)
        .join(Team, Team.id == TeamMember.team_id)
        .where(
            TeamMember.team_id.in_(team_ids),
            TeamMember.user_id == user.id,
            TeamMember.role.in_(("maintainer", "owner")),
            Team.deleted_at.is_(None),
        )
        .limit(1)
    )
    return lead is not None


def team_role(db: Session, user: User, team: Team) -> str | None:
    if user.is_admin:
        return "owner"
    return db.scalar(
        select(TeamMember.role).where(TeamMember.team_id == team.id, TeamMember.user_id == user.id)
    )


def require_team_role(db: Session, user: User, team: Team, needed: str) -> str:
    role = team_role(db, user, team)
    if role is None or TEAM_RANK[role] < TEAM_RANK[needed]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"This needs team {needed} rights")
    if is_bot(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Bots have read-only access")
    return role
