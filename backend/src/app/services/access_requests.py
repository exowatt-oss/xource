"""Asking for, and deciding, access to a space.

**Non-disclosure.** Submitting a request never reveals whether its target
exists. The response is identical whether the item exists, is hidden from the
caller, does not exist, or the caller already has access. The requester's own
list also looks the same: a hidden-but-real target becomes an actionable
request for the space's managers, while a missing target (or a second request
into a space that already has one pending from the same person) becomes an
inert *receipt* that only its author sees. Both read as "pending". A manager's
decision is the first thing that tells the requester the target is real.

Requests and decisions are audited (``access.*``) in the same transaction.
The activity feed shows those events to space managers and admins only, so a
requester cannot use their own activity as an oracle either.

Approval reuses :func:`app.services.sharing.upsert_grant`, never lowering a
role the person already has. In a personal-folders space a viewer/editor
grant still confines the person to their own folder (``access.Scope``).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Hashable
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AccessRequest, Node, Space, User
from app.schemas.access import AccessRequestOut
from app.services import access, audit, notifications, sharing, tree

# Per person, across all targets (existing or not), per process.
RATE_LIMIT = 20
RATE_WINDOW_S = 600.0
# Beyond this many pending requests/receipts a person's new ones are dropped
# silently (still answered 202), so the cap itself is not an oracle.
MAX_PENDING = 50
MESSAGE_MAX = 1000


class RateLimiter:
    """Sliding-window limiter keyed by user id. In-process: a multi-worker
    deployment multiplies the allowance by the worker count, which is fine
    for a nuisance limit (the pending cap bounds storage regardless)."""

    def __init__(self, limit: int, window_s: float):
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[Hashable, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: Hashable) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] > self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter(RATE_LIMIT, RATE_WINDOW_S)


@dataclass
class Target:
    """What a request resolves to, for the server only."""

    space: Space | None
    node: Node | None


def _resolve(db: Session, target_type: str, target_id: UUID) -> Target:
    if target_type == "node":
        node = db.get(Node, target_id)
        if node is None or node.deleted_at is not None:
            return Target(None, None)
        space = db.get(Space, node.space_id)
        if space is None or space.deleted_at is not None:
            return Target(None, None)
        return Target(space, node)
    space = db.get(Space, target_id)
    if space is None or space.deleted_at is not None:
        return Target(None, None)
    return Target(space, None)


def _already_has(db: Session, user: User, target: Target, role: str) -> bool:
    assert target.space is not None
    current = access.space_role(db, user, target.space)
    if current is None or access.RANK[current] < access.RANK[role]:
        return False
    if target.node is None:
        return True
    scope = access.space_scope(db, user, target.space, "viewer")
    return scope.can_see(db, target.node)


def submit(
    db: Session,
    user: User,
    *,
    target_type: str,
    target_id: UUID,
    role: str,
    message: str | None,
    request: Request | None = None,
) -> None:
    """Record a request. Returns nothing the caller may echo back; commits."""
    if access.is_bot(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Bots cannot request access")
    if not limiter.allow(user.id):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "Too many access requests; try again later"
        )
    message = (message or "").strip()[:MESSAGE_MAX] or None
    target = _resolve(db, target_type, target_id)
    if target.space is not None and _already_has(db, user, target, role):
        return
    pending = db.scalar(
        select(func.count()).where(
            AccessRequest.requester_id == user.id, AccessRequest.status == "pending"
        )
    )
    if (pending or 0) >= MAX_PENDING:
        return
    duplicate = db.scalar(
        select(AccessRequest.id).where(
            AccessRequest.requester_id == user.id,
            AccessRequest.target_id == target_id,
            AccessRequest.status == "pending",
        )
    )
    if duplicate is not None:
        return
    space = target.space
    if space is not None:
        open_in_space = db.scalar(
            select(AccessRequest.id).where(
                AccessRequest.requester_id == user.id,
                AccessRequest.space_id == space.id,
                AccessRequest.status == "pending",
            )
        )
        if open_in_space is not None:
            space = None  # One actionable request per space; this becomes a receipt.
    row = AccessRequest(
        space_id=space.id if space is not None else None,
        node_id=target.node.id if space is not None and target.node is not None else None,
        target_type=target_type,
        target_id=target_id,
        requester_id=user.id,
        requested_role=role,
        message=message,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:  # A concurrent duplicate won the race; same answer.
        return
    audit.record(
        db,
        user,
        "access.request",
        f"Requested {role} access",
        request=request,
        space_id=row.space_id,
        node_id=row.node_id,
        details={
            "request_id": str(row.id),
            "role": role,
            "actionable": row.space_id is not None,
            "target_type": target_type,
            "target_id": str(target_id),
        },
    )
    if space is not None:
        payload = request_payload(row, space, target.node, user)
        for manager in notifications.space_managers(db, space, exclude=user.id):
            notifications.notify(db, manager.id, "access.requested", payload)
    db.commit()


def request_payload(row: AccessRequest, space: Space, node: Node | None, requester: User) -> dict:
    """What a manager is told about a request (they can already see it all)."""
    return {
        "request_id": str(row.id),
        "space_id": str(space.id),
        "space_name": space.name,
        "space_visibility": space.visibility,
        "node_id": str(node.id) if node is not None else None,
        "node_name": node.name if node is not None else None,
        "node_kind": node.kind if node is not None else None,
        "requester_email": requester.email,
        "requester_name": requester.name,
        "role": row.requested_role,
        "message": row.message,
        "personal_folders": bool(space.personal_folders),
    }


class AlreadyDecided(HTTPException):
    def __init__(self, row: AccessRequest):
        super().__init__(status.HTTP_409_CONFLICT, f"This request was already {row.status}")
        self.row = row


def _lock(db: Session, request_id: UUID) -> AccessRequest:
    row = db.scalar(
        select(AccessRequest)
        .where(AccessRequest.id == request_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None or row.space_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")
    return row


def decide(
    db: Session,
    actor: User,
    request_id: UUID,
    *,
    approve: bool,
    role: str | None = None,
    request: Request | None = None,
    via: str | None = None,
) -> AccessRequest:
    """Approve or deny as a manager of the request's space. Flushes, never
    commits. Raises :class:`AlreadyDecided` (409) if no longer pending."""
    if access.is_bot(actor):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Bots cannot decide access requests")
    row = _lock(db, request_id)
    space = access.get_space(db, row.space_id)
    access.require_space_role(db, actor, space, "manager")
    if row.status != "pending":
        raise AlreadyDecided(row)
    requester = db.get(User, row.requester_id)
    if requester is None:  # pragma: no cover - cascade deletes the request
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")
    role = role or row.requested_role
    if role not in ("viewer", "editor"):
        raise HTTPException(422, "Approve as viewer or editor; use sharing for manager")
    extra = {"access_request_id": str(row.id), **({"via": via} if via else {})}
    if approve:
        sharing.upsert_grant(
            db,
            actor,
            space,
            principal_type="user",
            principal_id=requester.id,
            label=requester.email,
            role=role,
            request=request,
            only_upgrade=True,
            notify=False,
            audit_details=extra,
        )
    row.status = "approved" if approve else "denied"
    row.decided_by_id = actor.id
    row.decided_at = tree.now()
    row.decided_role = role if approve else None
    audit.record(
        db,
        actor,
        "access.approve" if approve else "access.deny",
        (
            f"Approved {requester.email} as {role} on {space.name}"
            if approve
            else f"Denied {requester.email}'s access request to {space.name}"
        ),
        request=request,
        space_id=space.id,
        node_id=row.node_id,
        details={"request_id": str(row.id), "role": role if approve else None, **extra},
    )
    notifications.notify(db, requester.id, "access.decided", decided_payload(row, space, actor))
    _close_receipts(db, row, space)
    db.flush()
    return row


def decided_payload(row: AccessRequest, space: Space, actor: User) -> dict:
    """What the requester is told. A denial names nothing they could not see."""
    payload = {
        "request_id": str(row.id),
        "decision": row.status,
        "target_type": row.target_type,
        "target_id": str(row.target_id),
        "actor": audit.actor_label(actor),
    }
    if row.status == "approved":
        payload.update(
            role=row.decided_role,
            space_id=str(space.id),
            space_name=space.name,
            node_id=str(row.node_id) if row.node_id else None,
            personal_folders=bool(space.personal_folders),
        )
    return payload


def _close_receipts(db: Session, row: AccessRequest, space: Space) -> None:
    """Receipts the same person filed into this space follow the decision."""
    receipts = db.scalars(
        select(AccessRequest).where(
            AccessRequest.requester_id == row.requester_id,
            AccessRequest.space_id.is_(None),
            AccessRequest.status == "pending",
        )
    ).all()
    for receipt in receipts:
        target = _resolve(db, receipt.target_type, receipt.target_id)
        if target.space is not None and target.space.id == space.id:
            receipt.status = row.status
            receipt.decided_by_id = row.decided_by_id
            receipt.decided_at = row.decided_at
            receipt.decided_role = row.decided_role


def cancel(db: Session, user: User, request_id: UUID, request: Request | None = None) -> None:
    row = db.scalar(select(AccessRequest).where(AccessRequest.id == request_id).with_for_update())
    if row is None or row.requester_id != user.id or access.is_bot(user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Request not found")
    if row.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, f"This request was already {row.status}")
    row.status = "cancelled"
    row.decided_at = tree.now()
    audit.record(
        db,
        user,
        "access.cancel",
        "Cancelled an access request",
        request=request,
        space_id=row.space_id,
        node_id=row.node_id,
        details={"request_id": str(row.id)},
    )
    db.commit()


def decidable(db: Session, user: User, limit: int = 200) -> list[AccessRequest]:
    """Pending actionable requests in spaces this person manages."""
    if access.is_bot(user):
        return []
    managed = [sid for sid, role in access.roles_by_space(db, user).items() if role == "manager"]
    if not managed:
        return []
    return list(
        db.scalars(
            select(AccessRequest)
            .where(AccessRequest.space_id.in_(managed), AccessRequest.status == "pending")
            .order_by(AccessRequest.created_at.desc())
            .limit(limit)
        )
    )


def mine(db: Session, user: User, limit: int = 50) -> list[AccessRequest]:
    return list(
        db.scalars(
            select(AccessRequest)
            .where(AccessRequest.requester_id == user.id)
            .order_by(AccessRequest.created_at.desc())
            .limit(limit)
        )
    )


def view(db: Session, row: AccessRequest, *, manager_view: bool, user: User) -> AccessRequestOut:
    """A request as ``user`` may see it: names for managers, or for the
    requester once approved and only what they can see right now."""
    out = AccessRequestOut(
        id=row.id,
        status=row.status,
        requested_role=row.requested_role,
        decided_role=row.decided_role,
        message=row.message,
        created_at=row.created_at,
        decided_at=row.decided_at,
        target_type=row.target_type,
        target_id=row.target_id,
    )
    if manager_view:
        space = db.get(Space, row.space_id)
        node = db.get(Node, row.node_id) if row.node_id else None
        requester = db.get(User, row.requester_id)
        out.space_id, out.space_name = space.id, space.name
        out.personal_folders = bool(space.personal_folders)
        if node is not None:
            out.node_id, out.node_name, out.node_kind = node.id, node.name, node.kind
        if requester is not None:
            out.requester_email, out.requester_name = requester.email, requester.name
        return out
    if row.status == "approved" and row.space_id is not None:
        # Only what the requester can see right now.
        space = db.get(Space, row.space_id)
        if space is not None and space.deleted_at is None:
            if access.space_role(db, user, space) is not None:
                out.space_id, out.space_name = space.id, space.name
                out.personal_folders = access.is_confined(space, access.space_role(db, user, space))
            if row.node_id is not None:
                try:
                    node, _ = access.get_live_node_for(db, user, row.node_id)
                except HTTPException:
                    node = None
                if node is not None:
                    out.node_id, out.node_name, out.node_kind = node.id, node.name, node.kind
    return out
