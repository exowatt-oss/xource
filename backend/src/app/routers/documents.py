"""Authorized versioned document previews, text, thumbnails and retries."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import get_current_user
from app.models import FileVersion, Node, User
from app.schemas.documents import DocumentPreviewOut, DocumentTextOut
from app.services import access, audit, documents

router = APIRouter(tags=["documents"])
NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _authorized_version(
    db: Session, user: User, node_id: UUID, number: int | None, needed: str
) -> tuple[Node, FileVersion]:
    # Do not read even the *cache's existence* before this permission check.
    node, _ = access.get_live_node_for(db, user, node_id, needed)
    if node.kind != "file":
        raise HTTPException(status.HTTP_409_CONFLICT, "Folders have no content")
    if number is None:
        version = node.current_version
    else:
        version = db.scalar(
            select(FileVersion).where(FileVersion.node_id == node.id, FileVersion.number == number)
        )
    if version is None or version.node_id != node.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Version not found")
    return node, version


def _index_authorized(db, user, node, selected, needed, *, force=False):
    number, node_id = selected.number, node.id
    try:
        index = documents.index_document(db, node, selected, force=force)
    except documents.DocumentRetry:
        # A 503 normally rolls back the dependency's session. Persist only safe
        # attempt/backoff metadata, after the same final live authorization.
        db.expire_all()
        _authorized_version(db, user, node_id, number, needed)
        db.commit()
        raise
    db.expire_all()
    node, selected = _authorized_version(db, user, node_id, number, needed)
    return node, selected, index


def _text_out(node: Node, version: FileVersion, index) -> dict:
    return {
        "node_id": node.id,
        "version": version.number,
        "kind": index.preview.get("kind", "unsupported"),
        "status": index.status,
        "reason": index.reason,
        "truncated": index.preview.get("truncated", False),
        "text": index.text,
    }


@router.get("/nodes/{node_id}/preview", response_model=DocumentPreviewOut)
def preview(
    node_id: UUID,
    response: Response,
    version: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Preview OOXML structures or plain text for a version you may see.

    PDF returns kind=pdf and extracted text; display the *actual PDF* via
    /api/nodes/{id}/content?version=N&disposition=inline instead.
    """
    response.headers.update(NO_STORE)
    node, selected = _authorized_version(db, user, node_id, version, "viewer")
    node, selected, index = _index_authorized(db, user, node, selected, "viewer")
    db.commit()
    return DocumentPreviewOut(
        **_text_out(node, selected, index),
        paragraphs=index.preview.get("paragraphs", []),
        sheets=index.preview.get("sheets", []),
        slides=index.preview.get("slides", []),
    )


@router.get("/nodes/{node_id}/text", response_model=DocumentTextOut)
def text(
    node_id: UUID,
    response: Response,
    version: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get bounded extracted plain text and extraction status."""
    response.headers.update(NO_STORE)
    node, selected = _authorized_version(db, user, node_id, version, "viewer")
    node, selected, index = _index_authorized(db, user, node, selected, "viewer")
    db.commit()
    return DocumentTextOut(**_text_out(node, selected, index))


@router.get("/nodes/{node_id}/thumbnail", response_class=Response)
def thumbnail(
    node_id: UUID,
    version: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Small PNG from the first page of a PDF or the first image frame."""
    node, selected = _authorized_version(db, user, node_id, version, "viewer")
    result, png = documents.cached_thumbnail_for(db, node, selected)
    # Rendering may outlast an access change. Expire ORM policy state and
    # authorize again before committing a derivative or returning private bytes.
    number = selected.number
    db.expire_all()
    node, selected = _authorized_version(db, user, node_id, number, "viewer")
    if result != "ready":
        db.commit()  # safe negative/backoff marker, never private bytes or parser stderr
        codes = {
            "unsupported": status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "oversize": status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            "unavailable": status.HTTP_503_SERVICE_UNAVAILABLE,
            "disabled": status.HTTP_503_SERVICE_UNAVAILABLE,
        }
        headers = {**NO_STORE, **({"Retry-After": "1"} if result == "unavailable" else {})}
        raise HTTPException(
            codes.get(result, status.HTTP_422_UNPROCESSABLE_CONTENT),
            documents.PROCESSING_DISABLED if result == "disabled" else "Thumbnail unavailable",
            headers=headers,
        )
    db.commit()
    return Response(content=png, media_type="image/png", headers=NO_STORE)


@router.post("/nodes/{node_id}/index", response_model=DocumentTextOut)
def retry_index(
    node_id: UUID,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Editor-only retry for the current version; audit and cache commit atomically."""
    response.headers.update(NO_STORE)
    node, selected = _authorized_version(db, user, node_id, None, "editor")
    node, selected, index = _index_authorized(db, user, node, selected, "editor", force=True)
    audit.record(
        db,
        user,
        "file.index",
        f"Re-indexed {node.name} (version {selected.number}: {index.status})",
        request=request,
        space_id=node.space_id,
        node_id=node.id,
        details={"version": selected.number, "status": index.status},
    )
    db.commit()
    return DocumentTextOut(**_text_out(node, selected, index))
