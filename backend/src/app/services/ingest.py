"""The one path every byte takes into Xource.

Browser uploads and new versions call :func:`save_file`, so de-duplication,
versioning and the audit log behave identically for each saved file.
"""

from __future__ import annotations

import tempfile
import threading
import time
from collections.abc import Callable
from typing import Any, BinaryIO
from uuid import UUID

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Blob, FileVersion, Node, User
from app.services import audit, safeguards, storage, tree

_RESTORE_SLOTS = threading.BoundedSemaphore(2)


def save_file(
    db: Session,
    user: User,
    *,
    space_id: UUID,
    parent_id: UUID | None,
    name: str,
    source: BinaryIO,
    declared_type: str | None = None,
    message: str | None = None,
    node: Node | None = None,
    request: Request | None = None,
    display_path: str | None = None,
    details: dict[str, Any] | None = None,
    before_write: Callable[[], tuple[Node | None, UUID | None]] | None = None,
) -> tuple[Node, str]:
    """Store ``source`` and add it as a file (or a new version of ``node``).

    Returns ``(node, outcome)`` where outcome is ``created``, ``versioned`` or
    ``unchanged``. The caller commits; the audit event is part of the same
    transaction.
    """
    limit = get_settings().max_upload_mb * 1024 * 1024
    try:
        spool = storage.HashedSpool(source, limit)
    except ValueError as exc:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc)) from exc
    try:
        content_type = tree.guess_type(name, declared_type)
        scan_result = safeguards.scan_upload(spool.file, spool.size)
        key = storage.put_blob(spool, content_type)
        # Remote callers reauthorize only after download/hash/scan/storage work.
        # No policy/quota lock spans these potentially slow external operations.
        if before_write is not None:
            node, parent_id = before_write()
        safeguards.lock_write_space(db, space_id)
        blob = tree.upsert_blob(db, spool.sha256, spool.size, content_type, key)
        node, outcome = tree.write_file(
            db, user, space_id, parent_id, name, blob, content_type, message, node=node
        )
    finally:
        spool.close()
    if outcome != "unchanged":
        v = node.current_version
        audit.record(
            db,
            user,
            "file.upload" if outcome == "created" else "file.version",
            (
                f"Uploaded {display_path or node.name}"
                if outcome == "created"
                else f"Uploaded version {v.number} of {node.name}"
            ),
            request=request,
            space_id=space_id,
            node_id=node.id,
            details={
                "version": v.number,
                "sha256": v.blob_sha256,
                "size": v.size,
                "message": message,
                **(details or {}),
                "malware_scan": scan_result,
            },
        )
    return node, outcome


def revalidate_restore_bytes(blob: Blob, version: FileVersion) -> str:
    """Recheck retained bytes before a version restore, without writing history.

    Old versions may predate ClamAV. Fetch only when scanning is enabled;
    bound bytes read from the store, verify both version and blob metadata,
    then apply the same fail-closed scan used by new uploads.
    """
    cfg = get_settings()
    if not cfg.clamav_enabled:
        return "disabled"
    if not _RESTORE_SLOTS.acquire(blocking=False):
        raise HTTPException(503, "Restore scanners busy; retry shortly")
    try:
        return _scan_restore_bytes(blob, version)
    finally:
        _RESTORE_SLOTS.release()


def _scan_restore_bytes(blob: Blob, version: FileVersion) -> str:
    cfg = get_settings()
    if version.size < 0 or version.size != blob.size or version.blob_sha256 != blob.sha256:
        raise HTTPException(503, "Retained file metadata does not match; restore was not created")
    limit = cfg.clamav_max_scan_mb * 1024 * 1024
    if version.size > limit:
        raise HTTPException(413, "File exceeds the configured malware scan size limit")

    # HashedSpool accepts a file-like source. First stage the streamed bytes
    # with an actual-byte bound, then let it compute the hash into its own
    # bounded spool; neither disk spool can exceed the scan limit.
    deadline = time.monotonic() + cfg.clamav_timeout_s
    with tempfile.SpooledTemporaryFile(max_size=8 * storage.CHUNK) as source:
        stream = None
        try:
            stream = storage.stream_blob(blob.storage_key)
            size = 0
            for chunk in stream:
                if time.monotonic() > deadline:
                    raise HTTPException(
                        503, "Retained file read timed out; restore was not created"
                    )
                size += len(chunk)
                if size > version.size:
                    raise HTTPException(503, "Retained file bytes failed verification")
                source.write(chunk)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - storage failures must fail closed
            raise HTTPException(503, "Retained file unavailable; restore was not created") from exc
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                try:
                    close()
                except Exception as exc:  # noqa: BLE001 - fail closed on close failures too
                    raise HTTPException(503, "Retained file stream could not be closed") from exc
        source.seek(0)
        spool = storage.HashedSpool(source, limit)
        try:
            if spool.size != version.size or spool.sha256 != version.blob_sha256:
                raise HTTPException(503, "Retained file bytes failed verification")
            if time.monotonic() > deadline:
                raise HTTPException(503, "Retained file read timed out; restore was not created")
            try:
                return safeguards.scan_upload(spool.file, spool.size)
            except HTTPException as exc:
                if exc.status_code == 422:
                    raise HTTPException(422, "Restore rejected: malware detected") from exc
                if exc.status_code == 503:
                    raise HTTPException(
                        503, "Malware scanner unavailable; restore was not created"
                    ) from exc
                raise
        finally:
            spool.close()
