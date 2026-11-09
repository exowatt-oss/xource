"""Files and folders: browse, upload, version history, download, move, trash."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from app.db import get_db
from app.deps import get_current_user
from app.models import Blob, ExternalLink, FileVersion, Node, User
from app.schemas.xource import (
    ChildrenOut,
    FolderIn,
    NodeDetailOut,
    NodeOut,
    NodePatch,
    UploadResult,
    VersionOut,
)
from app.services import access, audit, ingest, storage, tree
from app.services.views import crumbs, node_out, space_out, space_stats, version_counts

router = APIRouter(tags=["files"])

# Types a browser may render inline on our origin without running code.
# Anything else is either served as plain text (so the UI can show it) or
# forced to download. Never inline HTML / SVG / XML from users.
_INLINE_SAFE = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/avif",
    "image/bmp",
}
_TEXTUAL_PREFIXES = ("text/",)
_TEXTUAL = {
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-yaml",
    "application/yaml",
    "application/toml",
    "application/x-sh",
    "application/sql",
    "image/svg+xml",
}


def _children_query(scope: access.Scope, parent_id: UUID | None):
    parent_clause = Node.parent_id.is_(None) if parent_id is None else Node.parent_id == parent_id
    return (
        select(Node)
        .where(
            Node.space_id == scope.space.id,
            parent_clause,
            Node.deleted_at.is_(None),
            scope.node_clause(),
        )
        .order_by((Node.kind == "file").asc(), func.lower(Node.name))
    )


@router.get("/spaces/{space_id}/children", response_model=ChildrenOut)
def list_children(
    space_id: UUID,
    parent_id: UUID | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Contents of a folder (or the space root). Folders first, then files, A→Z.

    In a personal-folders space a member's root listing holds just their own
    folder, which is created the first time they open the space."""
    space = access.get_space(db, space_id)
    scope = access.space_scope(db, user, space, "viewer", create=True)
    if scope.created:
        db.commit()
    folder = tree.require_folder(db, space.id, parent_id)
    if folder is not None:
        scope.require_visible(db, folder, "Folder")
    items = db.scalars(_children_query(scope, parent_id)).unique().all()
    counts = version_counts(db, [n.id for n in items if n.kind == "file"])
    stats = space_stats(db, [space.id], scope.node_clause()).get(space.id, (0, 0))
    space_info = space_out(
        space, scope.role, stats=stats, my_folder_id=scope.root.id if scope.root else None
    )
    space_info.can_delete = access.can_delete_space(db, user, space)
    return ChildrenOut(
        space=space_info,
        folder=node_out(folder) if folder else None,
        path=crumbs(space, tree.path_of(db, folder)),
        items=[node_out(n, counts.get(n.id)) for n in items],
    )


@router.post("/spaces/{space_id}/folders", response_model=NodeOut, status_code=201)
def create_folder(
    space_id: UUID,
    body: FolderIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    space = access.get_space(db, space_id)
    scope = access.space_scope(db, user, space, "editor", create=True)
    scope.write_target(db, body.parent_id)
    if tree.find_child(db, space.id, body.parent_id, body.name.strip()) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Something with that name already exists")
    folder, _ = tree.make_folder(db, user, space.id, body.parent_id, body.name)
    audit.record(
        db,
        user,
        "folder.create",
        f"Created folder {folder.name}",
        request=request,
        space_id=space.id,
        node_id=folder.id,
    )
    db.commit()
    return node_out(folder)


@router.post("/spaces/{space_id}/files", response_model=UploadResult, status_code=201)
def upload_files(
    space_id: UUID,
    request: Request,
    files: Annotated[list[UploadFile], File(description="One or more files")],
    parent_id: Annotated[UUID | None, Form()] = None,
    message: Annotated[str | None, Form(max_length=2000)] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Upload files into a folder.

    A file whose name already exists becomes a **new version** of it; identical
    bytes are a no-op. A filename containing ``/`` (a folder upload's relative
    path, e.g. ``specs/rev-b/drawing.pdf``) creates the intermediate folders.

    In a personal-folders space a member's upload always lands in their own
    folder: a name like ``specs/drawing.pdf`` with no ``parent_id`` resolves
    to ``alice/specs/drawing.pdf`` (a leading ``alice/`` is accepted too);
    a ``parent_id`` outside their folder is 404.
    """
    space = access.get_space(db, space_id)
    scope = access.space_scope(db, user, space, "editor", create=True)
    asked_parent_id, parent_id = parent_id, scope.upload_parent(db, parent_id)
    results: list[Node] = []
    tally = {"created": 0, "versioned": 0, "unchanged": 0}
    for upload in files:
        raw = (upload.filename or "untitled").replace("\\", "/")
        parts = [p for p in raw.split("/") if p not in ("", ".")]
        if not parts or ".." in parts:
            raise HTTPException(422, f"Bad file name {raw!r}")
        parts = scope.upload_parts(asked_parent_id, parts)
        folder_id = tree.ensure_path(db, user, space.id, parent_id, parts[:-1])
        if scope.confined:  # never write into a folder the member can't see
            scope.write_target(db, folder_id)
        node, outcome = ingest.save_file(
            db,
            user,
            space_id=space.id,
            parent_id=folder_id,
            name=parts[-1],
            source=upload.file,
            declared_type=upload.content_type,
            message=message,
            request=request,
            display_path="/".join(parts),
        )
        tally[outcome] += 1
        results.append(node)
    db.commit()
    counts = version_counts(db, [n.id for n in results])
    return UploadResult(items=[node_out(n, counts.get(n.id)) for n in results], **tally)


@router.get("/nodes/{node_id}", response_model=NodeDetailOut)
def get_node(node_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    node, role = access.get_node_for(db, user, node_id, "viewer")
    space = access.get_space(db, node.space_id)
    count = version_counts(db, [node.id]).get(node.id)
    return NodeDetailOut(
        **node_out(node, count).model_dump(),
        path=crumbs(space, tree.path_of(db, node)),
        space=space_out(space, role),
        my_role=role,
        source=db.execute(
            select(ExternalLink).where(ExternalLink.node_id == node.id)
        ).scalar_one_or_none(),
    )


@router.patch("/nodes/{node_id}", response_model=NodeOut)
def update_node(
    node_id: UUID,
    body: NodePatch,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Rename and/or move within the same space (a personal-folders member:
    within their own folder)."""
    node, scope = access.node_scope(db, user, node_id, "editor")
    if node.deleted_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Restore the item from trash first")
    scope.require_not_root(node)
    new_name = tree.clean_name(body.name) if body.name is not None else node.name
    new_parent = node.parent_id
    if body.move_to_root or body.parent_id is not None:
        # Moves in a space run one at a time, so the destination and the
        # cycle check below are evaluated against a tree nobody else is
        # reshaping. Re-read the node under the lock.
        tree.lock_tree(db, node.space_id)
        db.refresh(node)
        if node.deleted_at is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Restore the item from trash first")
    if body.move_to_root:
        scope.write_target(db, None)
        new_parent = None
    elif body.parent_id is not None:
        dest = scope.write_target(db, body.parent_id)
        if node.kind == "folder" and dest.id in tree.descendant_ids(db, node.id):
            raise HTTPException(status.HTTP_409_CONFLICT, "Cannot move a folder into itself")
        new_parent = dest.id
    if new_name == node.name and new_parent == node.parent_id:
        return node_out(node)
    clash = tree.find_child(db, node.space_id, new_parent, new_name)
    if clash is not None and clash.id != node.id:
        raise HTTPException(status.HTTP_409_CONFLICT, f"{new_name!r} already exists there")

    old_path = "/".join(n.name for n in tree.path_of(db, node))
    old_parent = node.parent_id
    node.name, node.parent_id, node.updated_at = new_name, new_parent, tree.now()
    db.flush()
    new_path = "/".join(n.name for n in tree.path_of(db, node))
    action = "node.move" if new_parent != old_parent else "node.rename"
    audit.record(
        db,
        user,
        action,
        f"{'Moved' if action == 'node.move' else 'Renamed'} {old_path} → {new_path}",
        request=request,
        space_id=node.space_id,
        node_id=node.id,
        details={"from": old_path, "to": new_path},
    )
    tree.touch(db, old_parent, node.space_id)
    tree.touch(db, new_parent, node.space_id)
    db.commit()
    return node_out(node)


@router.delete("/nodes/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
def trash_node(
    node_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Move to trash. Nothing is ever destroyed: history and bytes are kept."""
    node, scope = access.node_scope(db, user, node_id, "editor")
    scope.require_not_root(node)
    tree.lock_tree(db, node.space_id)
    db.refresh(node)
    if node.deleted_at is not None:
        return
    path = "/".join(n.name for n in tree.path_of(db, node))
    count = tree.trash(db, user, node)
    audit.record(
        db,
        user,
        "node.trash",
        f"Moved {path} to trash",
        request=request,
        space_id=node.space_id,
        node_id=node.id,
        details={"items": count},
    )
    db.commit()


@router.post("/nodes/{node_id}/restore", response_model=NodeOut)
def restore_node(
    node_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    node, scope = access.node_scope(db, user, node_id, "editor")
    scope.require_not_root(node)
    tree.lock_tree(db, node.space_id)
    db.refresh(node)
    count = tree.restore(db, node, fallback_parent_id=scope.root.id if scope.confined else None)
    audit.record(
        db,
        user,
        "node.restore",
        f"Restored {node.name} from trash",
        request=request,
        space_id=node.space_id,
        node_id=node.id,
        details={"items": count},
    )
    db.commit()
    db.refresh(node)
    return node_out(node)


@router.get("/spaces/{space_id}/trash", response_model=list[NodeOut])
def list_trash(
    space_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Top-level trashed items (not the children that went with them)."""
    scope = access.space_scope(db, user, space_id, "viewer")
    parent = aliased(Node)
    rows = (
        db.scalars(
            select(Node)
            .outerjoin(parent, parent.id == Node.parent_id)
            .where(
                Node.space_id == space_id,
                Node.deleted_at.is_not(None),
                scope.node_clause(),
                or_(
                    parent.id.is_(None),
                    parent.deleted_at.is_(None),
                    and_(parent.deleted_at.is_not(None), parent.deleted_at != Node.deleted_at),
                ),
            )
            .order_by(Node.deleted_at.desc())
            .limit(500)
        )
        .unique()
        .all()
    )
    return [node_out(n) for n in rows]


# ---------------------------------------------------------------- versions


@router.get("/nodes/{node_id}/versions", response_model=list[VersionOut])
def list_versions(
    node_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """Full history, newest first. Versions are immutable."""
    node, _ = access.get_node_for(db, user, node_id, "viewer")
    rows = db.scalars(
        select(FileVersion)
        .where(FileVersion.node_id == node.id)
        .order_by(FileVersion.number.desc())
    ).all()
    return [VersionOut.model_validate(v) for v in rows]


@router.post("/nodes/{node_id}/versions", response_model=NodeOut, status_code=201)
def upload_version(
    node_id: UUID,
    request: Request,
    file: Annotated[UploadFile, File()],
    message: Annotated[str | None, Form(max_length=2000)] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Upload a new version of this exact file (keeps its name and place)."""
    node, _ = access.get_node_for(db, user, node_id, "editor")
    if node.kind != "file" or node.deleted_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Not a live file")
    node, _ = ingest.save_file(
        db,
        user,
        space_id=node.space_id,
        parent_id=node.parent_id,
        name=node.name,
        source=file.file,
        declared_type=file.content_type,
        message=message,
        node=node,
        request=request,
    )
    db.commit()
    return node_out(node, version_counts(db, [node.id]).get(node.id))


@router.post("/nodes/{node_id}/versions/{number}/restore", response_model=NodeOut)
def restore_version(
    node_id: UUID,
    number: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Make an old version current by adding a new version with its bytes —
    like ``git revert``: history only ever grows."""
    node, _ = access.get_node_for(db, user, node_id, "editor")
    if node.kind != "file":
        raise HTTPException(status.HTTP_409_CONFLICT, "Folders have no versions")
    if node.deleted_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Restore the item from trash first")
    node, _ = access.get_live_node_for(db, user, node_id, "editor")
    expected = (node.space_id, node.parent_id, node.name, node.current_version_id)
    old = db.scalar(
        select(FileVersion).where(FileVersion.node_id == node.id, FileVersion.number == number)
    )
    if old is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Version not found")
    blob = db.get(Blob, old.blob_sha256)
    if blob is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Retained blob unavailable")
    scan_result = ingest.revalidate_restore_bytes(blob, old)
    # Blob reads and scans can be slow: never write using a stale authorization
    # or overwrite a version/location change observed during that work.
    db.expire_all()
    node, _ = access.get_live_node_for(db, user, node_id, "editor")
    if expected != (node.space_id, node.parent_id, node.name, node.current_version_id):
        raise HTTPException(409, "File changed during restore; reopen and retry")
    node, _ = tree.write_file(
        db,
        user,
        node.space_id,
        node.parent_id,
        node.name,
        blob,
        old.content_type,
        f"Restored version {number}",
        node=node,
        restored_from=number,
    )
    audit.record(
        db,
        user,
        "file.restore_version",
        f"Restored {node.name} to version {number} (now version {node.current_version.number})",
        request=request,
        space_id=node.space_id,
        node_id=node.id,
        details={
            "restored_from": number,
            "version": node.current_version.number,
            "malware_scan": scan_result,
        },
    )
    db.commit()
    return node_out(node, version_counts(db, [node.id]).get(node.id))


@router.get("/nodes/{node_id}/content")
def download(
    node_id: UUID,
    request: Request,
    version: int | None = None,
    disposition: Annotated[str, Query(pattern="^(inline|attachment)$")] = "attachment",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Stream a file's bytes (current version, or ``?version=N``).

    ``disposition=inline`` is for in-app preview: only types that cannot run
    script are rendered as themselves; text-like types come back as
    ``text/plain``; everything else downloads.
    """
    node, _ = access.get_node_for(db, user, node_id, "viewer")
    if node.kind != "file":
        raise HTTPException(status.HTTP_409_CONFLICT, "Folders have no content")
    if version is None:
        v = node.current_version
    else:
        v = db.scalar(
            select(FileVersion).where(FileVersion.node_id == node.id, FileVersion.number == version)
        )
    if v is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Version not found")
    blob = db.get(Blob, v.blob_sha256)

    media_type, mode = v.content_type, disposition
    if disposition == "inline":
        if v.content_type in _INLINE_SAFE:
            pass
        elif v.content_type.startswith(_TEXTUAL_PREFIXES) or v.content_type in _TEXTUAL:
            media_type = "text/plain; charset=utf-8"
        else:
            mode = "attachment"
    if mode == "attachment":
        audit.record(
            db,
            user,
            "file.download",
            f"Downloaded {node.name} (version {v.number})",
            request=request,
            space_id=node.space_id,
            node_id=node.id,
            details={"version": v.number},
        )
        db.commit()

    headers = {
        "Content-Disposition": f"{mode}; filename*=UTF-8''{quote(node.name)}",
        "Content-Length": str(v.size),
        "ETag": f'"{v.blob_sha256}"',
        "X-Content-Type-Options": "nosniff",
        # Immutable bytes do not mean immutable access. Recheck permissions on
        # every request, including numbered history, after membership changes.
        "Cache-Control": "private, no-store",
        "Vary": "Cookie, Authorization",
        "X-Xource-Version": str(v.number),
    }
    if mode == "inline":
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return StreamingResponse(
        storage.stream_blob(blob.storage_key), media_type=media_type, headers=headers
    )


@router.delete("/nodes/{node_id}/purge", status_code=status.HTTP_204_NO_CONTENT)
def purge_node(
    node_id: UUID,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Delete a trashed file or folder forever, with every version and
    everything inside it (Xource admins only). Blob bytes stay in the object
    store (content-addressed and possibly shared); the audit log keeps a
    ``node.purge`` event."""
    node, _ = access.get_node_for(db, user, node_id, "viewer")
    access.require_admin(user)
    if node.deleted_at is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Move it to trash first")
    path = "/".join(n.name for n in tree.path_of(db, node))
    space_id, kind, name = node.space_id, node.kind, node.name
    # Nobody reshapes this tree while we decide; ``tree.purge`` then locks
    # the rows themselves and refuses if anything came back from the trash.
    tree.lock_tree(db, space_id)
    counts = tree.purge(db, tree.descendant_ids(db, node.id, live_only=False))
    audit.record(
        db,
        user,
        "node.purge",
        f"Permanently deleted {path}",
        request=request,
        space_id=space_id,
        node_id=node_id,
        details={"path": path, "kind": kind, "name": name, **counts},
    )
    db.commit()
