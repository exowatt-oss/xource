"""Notification outbox: written with the change, delivered later.

:func:`notify` adds a row to the caller's session, so a notification commits
or rolls back with the change that caused it (an approval, a share, a
mention). Rows are always readable in-app (``GET /api/notifications``).

:func:`deliver_forever` runs in the app lifespan and hands undelivered rows to
pluggable :class:`Channel` implementations (future optional adapters). Rows
are claimed with ``FOR UPDATE SKIP LOCKED`` and a short lease, so several
workers never deliver the same row, and no transaction stays open across a
network call. With no channel enabled the loop leaves rows untouched.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Notification, Space, SpaceGrant, Team, TeamMember, User
from app.services import tree

log = structlog.get_logger("notifications")

MAX_ATTEMPTS = 8
# Delivery is a nudge, not a record: never blast a backlog of stale messages
# when a channel is first configured or comes back after an outage.
DELIVERY_WINDOW = timedelta(hours=24)
LEASE = timedelta(minutes=5)
POLL_INTERVAL_S = 5.0
MAX_MANAGER_RECIPIENTS = 50

Outcome = Literal["delivered", "skipped"]


class Channel(Protocol):
    """An external delivery channel.

    ``deliver`` returns ``"delivered"``, or ``"skipped"`` when this channel can
    never deliver the row (for example the recipient has no external account); it
    raises for a transient failure, which is retried with backoff.
    """

    name: str

    def enabled(self) -> bool: ...

    def deliver(self, db: Session, notification: Notification, recipient: User) -> Outcome: ...


CHANNELS: list[Channel] = []


def register_channel(channel: Channel) -> None:
    if all(c.name != channel.name for c in CHANNELS):
        CHANNELS.append(channel)


def active_channels() -> list[Channel]:
    return [c for c in CHANNELS if c.enabled()]


def notify(db: Session, recipient_id: UUID, kind: str, payload: dict[str, Any]) -> Notification:
    """Queue a notification in the caller's transaction (never commits)."""
    row = Notification(recipient_user_id=recipient_id, kind=kind, payload=payload)
    db.add(row)
    return row


def space_managers(db: Session, space: Space, *, exclude: UUID | None = None) -> list[User]:
    """People who manage ``space`` explicitly: direct manager grants plus
    members of live teams with a manager grant. Xource admins are managers
    everywhere, so they are only asked when nobody else manages the space."""
    grants = db.execute(
        select(SpaceGrant.principal_type, SpaceGrant.principal_id).where(
            SpaceGrant.space_id == space.id, SpaceGrant.role == "manager"
        )
    ).all()
    user_ids = {pid for ptype, pid in grants if ptype == "user"}
    team_ids = [pid for ptype, pid in grants if ptype == "team"]
    if team_ids:
        user_ids.update(
            db.scalars(
                select(TeamMember.user_id)
                .join(Team, Team.id == TeamMember.team_id)
                .where(TeamMember.team_id.in_(team_ids), Team.deleted_at.is_(None))
            )
        )
    user_ids.discard(exclude)
    if not user_ids:
        stmt = select(User).where(User.is_admin.is_(True))
    else:
        stmt = select(User).where(User.id.in_(user_ids))
    people = db.scalars(stmt.order_by(User.email).limit(MAX_MANAGER_RECIPIENTS + 1)).all()
    return [p for p in people if p.id != exclude][:MAX_MANAGER_RECIPIENTS]


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(3600, 30 * 2 ** max(0, attempts - 1)))


def claim(db: Session, limit: int = 20) -> list[UUID]:
    """Lease up to ``limit`` due rows to this worker and commit the lease."""
    now = tree.now()
    rows = db.scalars(
        select(Notification)
        .where(
            Notification.delivered_at.is_(None),
            Notification.next_attempt_at.is_not(None),
            Notification.next_attempt_at <= now,
            Notification.created_at >= now - DELIVERY_WINDOW,
        )
        .order_by(Notification.created_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).all()
    for row in rows:
        row.attempts += 1
        row.next_attempt_at = now + LEASE
    ids = [row.id for row in rows]
    db.commit()
    return ids


def _deliver_one(db: Session, channels: list[Channel], notification_id: UUID) -> bool:
    row = db.get(Notification, notification_id)
    if row is None or row.delivered_at is not None:
        return False
    recipient = db.get(User, row.recipient_user_id)
    errors: list[str] = []
    retry = False
    for channel in channels if recipient is not None else []:
        try:
            outcome = channel.deliver(db, row, recipient)
        except Exception as exc:  # noqa: BLE001 - recorded and retried
            log.warning("notification_delivery_failed", channel=channel.name, kind=row.kind)
            errors.append(f"{channel.name}: {type(exc).__name__}: {str(exc)[:200]}")
            retry = True
            continue
        if outcome == "delivered":
            row.delivered_at = tree.now()
            row.channel = channel.name
            row.next_attempt_at = None
            row.last_error = None
            db.commit()
            return True
        errors.append(f"{channel.name}: skipped")
    row.last_error = "; ".join(errors)[:1000] or "recipient not found"
    if retry and row.attempts < MAX_ATTEMPTS:
        row.next_attempt_at = tree.now() + _backoff(row.attempts)
    else:
        row.next_attempt_at = None  # In-app only from here on.
    db.commit()
    return False


def deliver_batch(limit: int = 20) -> int:
    """One delivery pass. Returns how many rows were claimed."""
    from app.db import SessionLocal

    channels = active_channels()
    if not channels:
        return 0
    with SessionLocal() as db:
        ids = claim(db, limit)
    for notification_id in ids:
        with SessionLocal() as db:
            try:
                _deliver_one(db, channels, notification_id)
            except Exception as exc:  # noqa: BLE001 - one bad row never stops the loop
                db.rollback()
                log.warning("notification_row_failed", error=type(exc).__name__)
    return len(ids)


async def deliver_forever() -> None:
    """Lifespan task: poll the outbox. Failures never stop the app."""
    while True:
        try:
            claimed = await asyncio.to_thread(deliver_batch)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - e.g. schema not migrated yet
            log.warning("notification_delivery_unavailable", error=type(exc).__name__)
            claimed = 0
        await asyncio.sleep(0.2 if claimed else POLL_INTERVAL_S)


_NAME_FIELDS = ("space_name", "node_name")


def _uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value))
    except ValueError:
        return None


def redacted(db: Session, user: User, payload: dict, roles: dict[UUID, str] | None = None) -> dict:
    """The payload minus names the recipient can no longer see.

    Access changes after a notification is written; every reader (in-app
    list or optional delivery channel) re-checks it, never the stored snapshot.
    """
    from fastapi import HTTPException

    from app.services import access

    out = dict(payload)
    space_id = _uuid(out["space_id"]) if out.get("space_id") else None
    if space_id is not None:
        if roles is None:
            roles = access.roles_by_space(db, user)
        if space_id not in roles:
            for key in _NAME_FIELDS:
                out.pop(key, None)
            out["redacted"] = True
            return out
    node_id = _uuid(out["node_id"]) if out.get("node_id") else None
    if node_id is not None and out.get("node_name") is not None:
        try:
            access.get_live_node_for(db, user, node_id)
        except HTTPException:
            out.pop("node_name", None)
            out["redacted"] = True
    return out


def mine(db: Session, user: User, limit: int = 50) -> list[Notification]:
    return list(
        db.scalars(
            select(Notification)
            .where(Notification.recipient_user_id == user.id)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(limit)
        )
    )


def mark_read(db: Session, user: User, ids: list[UUID] | None) -> int:
    stmt = select(Notification).where(
        Notification.recipient_user_id == user.id, Notification.read_at.is_(None)
    )
    if ids is not None:
        stmt = stmt.where(Notification.id.in_(ids))
    rows = db.scalars(stmt).all()
    now: datetime = tree.now()
    for row in rows:
        row.read_at = now
    return len(rows)
