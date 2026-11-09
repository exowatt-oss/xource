"""Bounded structured document preview and extraction status responses."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

IndexStatus = Literal["ready", "skipped", "failed"]
DocumentKind = Literal["docx", "xlsx", "pptx", "text", "pdf", "image", "unsupported"]


class SheetCell(BaseModel):
    ref: str
    value: str


class SheetRow(BaseModel):
    number: int
    cells: list[SheetCell]


class SheetPreview(BaseModel):
    name: str
    rows: list[SheetRow]


class SlidePreview(BaseModel):
    number: int
    paragraphs: list[str]


class DocumentTextOut(BaseModel):
    node_id: UUID
    version: int
    kind: DocumentKind
    status: IndexStatus
    text: str = ""
    reason: str | None = None
    truncated: bool = False


class DocumentPreviewOut(DocumentTextOut):
    # PDF is deliberately *not* rendered as HTML; frontend uses the existing
    # authenticated /content?disposition=inline route for the real PDF viewer.
    paragraphs: list[str] = Field(default_factory=list)
    sheets: list[SheetPreview] = Field(default_factory=list)
    slides: list[SlidePreview] = Field(default_factory=list)
