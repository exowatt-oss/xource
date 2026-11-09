"""Access requests, the caller's notifications, and short-link resolution."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import User
from app.schemas.access import (
    AccessRequestAccepted,
    AccessRequestIn,
    AccessRequestOut,
    AccessRequestsOut,
    DecisionIn,
    MarkReadIn,
    NodeLocationOut,
    NotificationOut,
)
from app.services import access, access_requests, notifications

router = APIRouter(tags=["access requests"])


@router.post(
    "/access-requests",
    response_model=AccessRequestAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_access(
    body: AccessRequestIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Ask a space's managers for access.

    The answer is the same whether or not the item exists, is hidden from you,
    or you already have access, so it cannot be used to discover files."""
    target_type, target_id = (
        ("node", body.node_id) if body.node_id is not None else ("space", body.space_id)
    )
    access_requests.submit(
        db,
        user,
        target_type=target_type,
        target_id=target_id,
        role=body.role,
        message=body.message,
        request=request,
    )
    return AccessRequestAccepted()


@router.get("/access-requests", response_model=AccessRequestsOut)
def list_access_requests(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Your own requests, and pending requests in spaces you manage."""
    view = access_requests.view
    return AccessRequestsOut(
        mine=[view(db, r, manager_view=False, user=user) for r in access_requests.mine(db, user)],
        to_decide=[
            view(db, r, manager_view=True, user=user) for r in access_requests.decidable(db, user)
        ],
    )


@router.post("/access-requests/{request_id}/approve", response_model=AccessRequestOut)
def approve_access_request(
    request_id: UUID,
    request: Request,
    body: DecisionIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Grant the requester a role on the space (managers only)."""
    row = access_requests.decide(
        db, user, request_id, approve=True, role=body.role if body else None, request=request
    )
    db.commit()
    return access_requests.view(db, row, manager_view=True, user=user)


@router.post("/access-requests/{request_id}/deny", response_model=AccessRequestOut)
def deny_access_request(
    request_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = access_requests.decide(db, user, request_id, approve=False, request=request)
    db.commit()
    return access_requests.view(db, row, manager_view=True, user=user)


@router.post("/access-requests/{request_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
def cancel_access_request(
    request_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Withdraw one of your own pending requests."""
    access_requests.cancel(db, user, request_id, request)


# ---------------------------------------------------------------- notifications


@router.get("/notifications", response_model=list[NotificationOut])
def my_notifications(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Your recent notifications, newest first."""
    if access.is_bot(user):
        return []
    roles = access.roles_by_space(db, user)
    return [
        NotificationOut(
            id=n.id,
            kind=n.kind,
            payload=notifications.redacted(db, user, n.payload, roles),
            created_at=n.created_at,
            read_at=n.read_at,
        )
        for n in notifications.mine(db, user)
    ]


@router.post("/notifications/read")
def mark_notifications_read(
    body: MarkReadIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Mark the given notifications (or all of yours) as read."""
    if access.is_bot(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Bots have read-only access")
    count = notifications.mark_read(db, user, body.ids)
    db.commit()
    return {"updated": count}


# ---------------------------------------------------------------- short links


@router.get("/nodes/{node_id}/location", response_model=NodeLocationOut)
def node_location(
    node_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Where an item lives, for ``/n/<id>`` links. 404 if you cannot see it."""
    try:
        node, _ = access.get_live_node_for(db, user, node_id)
    except HTTPException as exc:
        # One answer for missing, trashed and hidden (some paths say "Space").
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Item not found") from None
        raise
    return NodeLocationOut(
        node_id=node.id, space_id=node.space_id, parent_id=node.parent_id, kind=node.kind
    )
