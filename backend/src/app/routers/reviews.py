"""File review, approval decisions, own access explanation and in-app mentions."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import Node, Space, User
from app.models.review import ReviewApprovalEvent, ReviewComment, ReviewMention
from app.schemas.reviews import (
    ApprovalIn,
    CommentIn,
    CommentOut,
    CommentResolutionIn,
    MentionOut,
    NodeAccessOut,
    ReviewOut,
)
from app.services import access, audit, notifications, reviews

router = APIRouter(tags=["reviews"])


@router.get("/nodes/{node_id}/access", response_model=NodeAccessOut)
def node_access(
    node_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    node, role = reviews.live_node(db, user, node_id)
    return reviews.explain_access(db, user, node, role)


@router.get("/nodes/{node_id}/comments", response_model=list[CommentOut])
def list_comments(
    node_id: UUID,
    version: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    node, _ = reviews.live_node(db, user, node_id)
    chosen = reviews.file_version(db, node, version)
    # The latest 300 in chronological display order, bounded for large files.
    comments = db.scalars(
        select(ReviewComment)
        .where(ReviewComment.node_id == node.id, ReviewComment.version_id == chosen.id)
        .order_by(ReviewComment.created_at.desc(), ReviewComment.id.desc())
        .limit(300)
    ).all()
    return reviews.comments_out(db, comments[::-1])


@router.post("/nodes/{node_id}/comments", response_model=CommentOut, status_code=201)
def add_comment(
    node_id: UUID,
    body: CommentIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    node, _ = reviews.live_node(db, user, node_id, "editor")
    chosen = reviews.file_version(db, node, body.version)
    recipients = reviews.known_recipients(db, node, body.mentions)
    comment = ReviewComment(
        node_id=node.id,
        version_id=chosen.id,
        version_number=chosen.number,
        author_id=user.id,
        author_label=audit.actor_label(user),  # names a linked agent
        body=body.body,
    )
    db.add(comment)
    db.flush()
    db.add_all(
        ReviewMention(comment_id=comment.id, user_id=person.id, email=person.email)
        for person in recipients
    )
    for person in recipients:
        if person.id != user.id:
            notifications.notify(
                db,
                person.id,
                "review.mention",
                {
                    "comment_id": str(comment.id),
                    "node_id": str(node.id),
                    "node_name": node.name,
                    "space_id": str(node.space_id),
                    "version": chosen.number,
                    "author": audit.actor_label(user),
                    "excerpt": comment.body[:280],
                },
            )
    audit.record(
        db,
        user,
        "review.comment",
        f"Commented on {node.name} v{chosen.number}",
        request=request,
        space_id=node.space_id,
        node_id=node.id,
        details={
            "comment_id": str(comment.id),
            "version": chosen.number,
            "mentions": len(recipients),
        },
    )
    db.commit()
    return reviews.comments_out(db, [comment])[0]


@router.patch("/comments/{comment_id}", response_model=CommentOut)
@router.put("/comments/{comment_id}", response_model=CommentOut)
def resolve_comment(
    comment_id: UUID,
    body: CommentResolutionIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    comment = db.get(ReviewComment, comment_id)
    if comment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Comment not found")
    node, _ = reviews.live_node(db, user, comment.node_id, "editor")
    if body.resolved != (comment.resolved_at is not None):
        comment.resolved_at = datetime.now(UTC) if body.resolved else None
        comment.resolved_by_id = user.id if body.resolved else None
        audit.record(
            db,
            user,
            "review.comment_resolve" if body.resolved else "review.comment_reopen",
            (
                f"{'Resolved' if body.resolved else 'Reopened'} comment on "
                f"{node.name} v{comment.version_number}"
            ),
            request=request,
            space_id=node.space_id,
            node_id=node.id,
            details={"comment_id": str(comment.id), "version": comment.version_number},
        )
        db.commit()
    return reviews.comments_out(db, [comment])[0]


@router.get("/nodes/{node_id}/review", response_model=ReviewOut)
def get_review(
    node_id: UUID,
    version: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    node, _ = reviews.live_node(db, user, node_id)
    chosen = reviews.file_version(db, node, version)
    events = db.scalars(
        select(ReviewApprovalEvent)
        .where(ReviewApprovalEvent.node_id == node.id, ReviewApprovalEvent.version_id == chosen.id)
        .order_by(ReviewApprovalEvent.created_at.desc(), ReviewApprovalEvent.id.desc())
        .limit(100)
    ).all()
    return reviews.review_out(node, chosen, events)


@router.post("/nodes/{node_id}/approval", response_model=ReviewOut, status_code=201)
def decide_approval(
    node_id: UUID,
    body: ApprovalIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    node, _ = reviews.live_node(db, user, node_id, "manager")
    chosen = reviews.file_version(db, node, body.version)
    event = ReviewApprovalEvent(
        node_id=node.id,
        version_id=chosen.id,
        version_number=chosen.number,
        approved=body.approved,
        actor_id=user.id,
        actor_label=audit.actor_label(user),
    )
    db.add(event)
    db.flush()
    audit.record(
        db,
        user,
        "review.approve" if body.approved else "review.unapprove",
        f"{'Approved' if body.approved else 'Withdrew approval of'} {node.name} v{chosen.number}",
        request=request,
        space_id=node.space_id,
        node_id=node.id,
        details={"decision_id": str(event.id), "version": chosen.number},
    )
    db.commit()
    return get_review(node.id, chosen.number, db, user)


@router.get("/review/mentions", response_model=list[MentionOut])
def my_mentions(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """In-app inbox for this caller only; inaccessible/trashed files vanish.

    There is deliberately no recipient selector, directory search or external
    notification integration. Revoking a team grant removes these entries on
    the very next request, without deleting the original comment.
    """
    visible = access.roles_by_space(db, user)
    if not visible:
        return []
    rows = db.execute(
        select(ReviewComment, Node)
        .join(ReviewMention, ReviewMention.comment_id == ReviewComment.id)
        .join(Node, Node.id == ReviewComment.node_id)
        .join(Space, Space.id == Node.space_id)
        .where(
            ReviewMention.user_id == user.id,
            access.visible_nodes(db, user, visible),
            access.untrashed_nodes(list(visible)),
            Node.deleted_at.is_(None),
            Space.deleted_at.is_(None),
        )
        .order_by(ReviewComment.created_at.desc(), ReviewComment.id.desc())
        .limit(100)
    ).all()
    allowed = []
    for comment, node in rows:
        # No stale permission cache; also hides descendants of trashed folders.
        try:
            reviews.live_node(db, user, node.id)
        except HTTPException as exc:
            if exc.status_code == status.HTTP_404_NOT_FOUND:
                continue
            raise
        allowed.append((comment, node))
    rendered = reviews.comments_out(db, [comment for comment, _ in allowed])
    return [
        MentionOut(comment=out, node_name=node.name, space_id=node.space_id)
        for out, (_, node) in zip(rendered, allowed, strict=True)
    ]
