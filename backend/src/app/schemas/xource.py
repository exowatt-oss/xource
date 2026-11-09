"""Request/response schemas for spaces, files, versions, teams and activity."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

SpaceRole = Literal["viewer", "editor", "manager"]
TeamRole = Literal["member", "maintainer", "owner"]
Visibility = Literal["restricted", "organization"]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- people


class PersonOut(ORM):
    id: UUID
    email: str
    name: str | None = None
    avatar_url: str | None = None


# ---------------------------------------------------------------- teams


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str | None = Field(default=None, max_length=64)
    description: str | None = None


class TeamPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None


class TeamMemberOut(ORM):
    user: PersonOut
    role: TeamRole
    added_at: datetime
    source: str | None = None


class TeamOut(ORM):
    id: UUID
    slug: str
    name: str
    description: str | None = None
    created_at: datetime
    source: str | None = None
    synced_at: datetime | None = None
    member_count: int = 0
    my_role: TeamRole | None = None
    deleted_at: datetime | None = None
    deleted_by: str | None = None


class TeamDetailOut(TeamOut):
    members: list[TeamMemberOut] = []


class TeamMemberIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: TeamRole = "member"


# ---------------------------------------------------------------- spaces


class SpaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str | None = Field(default=None, max_length=80)
    description: str | None = None
    visibility: Visibility = "restricted"


class SpacePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    visibility: Visibility | None = None
    # Confine everyone but managers to their own top-level folder.
    personal_folders: bool | None = None


class SpaceOut(ORM):
    id: UUID
    slug: str
    name: str
    description: str | None = None
    visibility: Visibility
    # Non-managers see only their own folder (see access.Scope).
    personal_folders: bool = False
    # Set when you are confined: the id of your own folder in the space.
    my_folder_id: UUID | None = None
    is_personal: bool = False
    created_at: datetime
    updated_at: datetime
    my_role: SpaceRole | None = None
    file_count: int | None = None
    total_bytes: int | None = None
    # Whether you may delete it (see access.can_delete_space); set on detail reads.
    can_delete: bool = False
    deleted_at: datetime | None = None
    deleted_by: str | None = None


class GrantIn(BaseModel):
    """Share with a person (``email``) or a team (``team_id``)."""

    email: str | None = None
    team_id: UUID | None = None
    role: SpaceRole = "viewer"


class GrantOut(BaseModel):
    id: UUID
    principal_type: Literal["user", "team"]
    principal_id: UUID
    role: SpaceRole
    label: str
    detail: str | None = None
    created_at: datetime


# ---------------------------------------------------------------- nodes


class FolderIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    parent_id: UUID | None = None


class NodePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    # Move: send the destination folder id, or `move_to_root: true`.
    parent_id: UUID | None = None
    move_to_root: bool = False


class VersionOut(ORM):
    id: UUID
    number: int
    size: int
    content_type: str
    blob_sha256: str
    message: str | None = None
    restored_from: int | None = None
    author_label: str
    created_at: datetime


class NodeOut(ORM):
    id: UUID
    space_id: UUID
    parent_id: UUID | None = None
    kind: Literal["file", "folder"]
    name: str
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    version: VersionOut | None = None
    version_count: int | None = None


class Crumb(BaseModel):
    id: UUID | None
    name: str


class SourceOut(ORM):
    """Metadata for an optional external import adapter (none shipped)."""

    source: str
    external_id: str
    external_version: str | None = None
    external_url: str | None = None
    meta: dict = Field(default_factory=dict)
    synced_at: datetime


class NodeDetailOut(NodeOut):
    path: list[Crumb]
    space: SpaceOut
    my_role: SpaceRole
    source: SourceOut | None = None


class ChildrenOut(BaseModel):
    space: SpaceOut
    folder: NodeOut | None
    path: list[Crumb]
    items: list[NodeOut]


class UploadResult(BaseModel):
    items: list[NodeOut]
    created: int
    versioned: int
    unchanged: int


class SearchHit(NodeOut):
    space_name: str
    path_text: str
    match_type: Literal["filename", "content"] = "filename"
    snippet: str | None = None


# ---------------------------------------------------------------- activity


class ActivityOut(ORM):
    id: int
    created_at: datetime
    actor_label: str
    action: str
    summary: str
    space_id: UUID | None = None
    node_id: UUID | None = None
    team_id: UUID | None = None
    details: dict


class StatsOut(BaseModel):
    spaces: int
    files: int
    folders: int
    versions: int
    logical_bytes: int
    stored_bytes: int
    users: int
    teams: int
    by_type: list[dict]
    uploads_by_day: list[dict]


# ---------------------------------------------------------------- graph


GraphNodeKind = Literal["space", "folder", "file", "type", "person", "repo"]
GraphEdgeKind = Literal["contains", "typed", "authored", "links", "home", "related"]


class GraphNode(BaseModel):
    id: str = Field(
        description="space:<id>, a folder/file id, repo:<owner>/<name>, type:<bucket> "
        "or person:<author>; pass it to GET /api/graph/nodes/{id}"
    )
    kind: GraphNodeKind
    label: str
    space_id: str | None = None
    parent_id: str | None = None
    depth: int = 0
    size: int | None = None
    content_type: str | None = None
    version_count: int | None = None
    updated_at: datetime | None = None
    # Repo nodes only.
    description: str | None = None
    html_url: str | None = None
    language: str | None = None
    archived: bool | None = None
    # Set by the traversal endpoint: "Space / folder / …".
    path: str | None = None


class GraphEdge(BaseModel):
    source: str
    target: str
    kind: GraphEdgeKind
    # ``related`` edges only: how strong the evidence is, and who decided.
    score: float | None = None
    status: Literal["suggested", "confirmed"] | None = None


class GraphCounts(BaseModel):
    spaces: int = 0
    folders: int = 0
    files: int = 0
    types: int = 0
    people: int = 0
    repos: int = 0


class GraphOut(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    truncated: bool
    counts: GraphCounts


class GraphNeighbor(BaseModel):
    edge: GraphEdgeKind
    direction: Literal["out", "in"] = Field(
        description="out: this node is the edge's source; in: its target"
    )
    node: GraphNode
    score: float | None = None
    status: Literal["suggested", "confirmed"] | None = None
    reasons: list[str] | None = None


class GraphNeighborGroup(BaseModel):
    edge: GraphEdgeKind
    direction: Literal["out", "in"]
    total: int


class GraphNodeOut(BaseModel):
    """One node and a page of its neighbors, for hopping node to node."""

    node: GraphNode
    neighbors: list[GraphNeighbor] = Field(description="Grouped by edge kind, then direction")
    groups: list[GraphNeighborGroup] = Field(description="Every group, with its full size")
    total: int
    next_cursor: str | None = Field(None, description="Pass as ?cursor= for the next page")
