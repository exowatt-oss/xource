"""Access requests, notifications and short-link resolution."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

RequestRole = Literal["viewer", "editor"]


class AccessRequestIn(BaseModel):
    """Ask for access to a file/folder (``node_id``) or a whole space."""

    node_id: UUID | None = None
    space_id: UUID | None = None
    role: RequestRole = "viewer"
    message: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def one_target(self) -> AccessRequestIn:
        if (self.node_id is None) == (self.space_id is None):
            raise ValueError("Send exactly one of node_id, space_id")
        return self


class AccessRequestAccepted(BaseModel):
    """Always the same body: it never says whether the target exists."""

    status: Literal["received"] = "received"
    detail: str = (
        "If this item exists and can be shared with you, the people who manage it have "
        "been asked. You'll be notified if access is granted."
    )


class DecisionIn(BaseModel):
    role: RequestRole | None = None


class AccessRequestOut(BaseModel):
    id: UUID
    status: Literal["pending", "approved", "denied", "cancelled"]
    requested_role: RequestRole
    decided_role: RequestRole | None = None
    message: str | None = None
    created_at: datetime
    decided_at: datetime | None = None
    target_type: Literal["space", "node"]
    target_id: UUID
    # Names are filled in only for someone who may see them: the space's
    # managers, or the requester once approved and able to see it.
    space_id: UUID | None = None
    space_name: str | None = None
    node_id: UUID | None = None
    node_name: str | None = None
    node_kind: Literal["file", "folder"] | None = None
    requester_email: str | None = None
    requester_name: str | None = None
    personal_folders: bool = False


class AccessRequestsOut(BaseModel):
    mine: list[AccessRequestOut]
    to_decide: list[AccessRequestOut]


class NotificationOut(BaseModel):
    id: UUID
    kind: str
    payload: dict[str, Any]
    created_at: datetime
    read_at: datetime | None = None


class MarkReadIn(BaseModel):
    ids: list[UUID] | None = Field(default=None, max_length=500)


class NodeLocationOut(BaseModel):
    node_id: UUID
    space_id: UUID
    parent_id: UUID | None
    kind: Literal["file", "folder"]
