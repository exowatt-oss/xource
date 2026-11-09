"""Write audit events. Call :func:`record` inside the same transaction as the
change it describes, so a change and its log entry commit or roll back together."""

from __future__ import annotations

import secrets
from typing import Any
from uuid import UUID

from fastapi import Request
from sqlalchemy.orm import Session

from app.models import AuditEvent, User


def actor_label(user: User) -> str:
    if user.actor and user.actor.startswith("system:"):
        return user.name or user.actor.removeprefix("system:")
    if user.actor:
        return f"{user.actor} (bot)"
    if agent := _agent(user):
        return f"{user.email} via agent {agent}"[:320]
    return user.email


def _agent(user: User) -> str | None:
    """``agent_links.agent_of`` (not imported: that module imports this one)."""
    return getattr(user, "_agent_slug", None)


def record(
    db: Session,
    user: User,
    action: str,
    summary: str,
    *,
    request: Request | None = None,
    space_id: UUID | None = None,
    node_id: UUID | None = None,
    team_id: UUID | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    via = _via(request)
    if via:
        details = {**(details or {}), "via": via}
    if agent := _agent(user):
        details = {**(details or {}), "agent": agent}
    event = AuditEvent(
        actor_id=None if user.actor else user.id,
        actor_label=actor_label(user),
        action=action,
        summary=summary[:500],
        space_id=space_id,
        node_id=node_id,
        team_id=team_id,
        details=details or {},
        ip=(request.client.host if request and request.client else None),
    )
    db.add(event)
    return event


# Calls the MCP tools make into this API in-process carry
# ``X-Xource-Via: <token> mcp:<tool>``. The token is random per process, so a
# direct caller can't stamp their own actions as an agent's (or hide that).
VIA_HEADER = "x-xource-via"
VIA_TOKEN = secrets.token_hex(16)


def _via(request: Request | None) -> str | None:
    if request is None:
        return None
    token, _, label = (request.headers.get(VIA_HEADER) or "").partition(" ")
    return label[:100] if label and secrets.compare_digest(token, VIA_TOKEN) else None
