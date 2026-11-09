"""Public contracts for the per-file access, discussion and review panel."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.xource import SpaceRole


class AccessReasonOut(BaseModel):
    source: Literal["admin", "direct", "team", "organization"]
    role: SpaceRole
    # Only the caller's own team names are included, never a member list.
    label: str


class AccessGrantOut(BaseModel):
    principal_type: Literal["user", "team"]
    principal_id: UUID
    role: SpaceRole
    label: str


class NodeAccessOut(BaseModel):
    node_id: UUID
    space_id: UUID
    role: SpaceRole
    reasons: list[AccessReasonOut]
    confined: bool = False
    # null for non-managers: the complete share list is not a viewer capability.
    grants: list[AccessGrantOut] | None = None


class CommentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=10_000)
    version: int | None = Field(default=None, gt=0)
    # Exact, previously known user UUIDs or @example.com emails, NOT a search
    # query. A recipient must currently be able to view this exact file.
    mentions: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("body")
    @classmethod
    def nonempty_body(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Comment cannot be blank")
        return value


class CommentResolutionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolved: bool


class MentionedPersonOut(BaseModel):
    user_id: UUID
    email: str


class CommentOut(BaseModel):
    id: UUID
    node_id: UUID
    version: int
    body: str
    author_id: UUID
    author_label: str
    created_at: datetime
    resolved: bool
    resolved_at: datetime | None
    resolved_by: str | None
    mentions: list[MentionedPersonOut]


class MentionOut(BaseModel):
    comment: CommentOut
    node_name: str
    space_id: UUID


class ApprovalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(gt=0)
    approved: bool


class ApprovalDecisionOut(BaseModel):
    id: UUID
    version: int
    approved: bool
    actor_label: str
    created_at: datetime


class ReviewOut(BaseModel):
    node_id: UUID
    version: int
    current_version: int
    approved: bool
    # pending = no decision for this version; not_approved = explicitly revoked.
    status: Literal["pending", "approved", "not_approved"]
    decisions: list[ApprovalDecisionOut]
