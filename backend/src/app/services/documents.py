"""Bounded, version-keyed document extraction and backfill.

Never call index_document from a read route until the live node's space role
and version ownership have been checked. The cache is deliberately independent
of file_versions (which is immutable); content search joins *only* a current
version on a live node in a space the caller may currently view.
"""

from __future__ import annotations

import asyncio
import json
import os
import selectors
import signal
import subprocess
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from struct import unpack
from threading import BoundedSemaphore

import structlog
from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import Blob, FileVersion, Node, Space
from app.models.document import DocumentIndex, DocumentProcessingAttempt, DocumentThumbnail
from app.services import access, document_sandbox, storage

log = structlog.get_logger("documents")

# Defense in depth: configuration can *lower*, but never raise, this ceiling
# without revisiting the worker's own independent 25 MB input bound.
MAX_FILE_MB = 25
MAX_WORKER_OUTPUT = 2 * 1024 * 1024
# Parsing and blob reads share two slots. Never queue a request or background
# indexer while it holds a DB session waiting for a parser to become free.
_PROCESSORS = BoundedSemaphore(2)
MAX_THUMBNAIL_BYTES = MAX_WORKER_OUTPUT
MAX_THUMBNAIL_SIDE = 512  # PDFium dimensions are bounded before native allocation
HISTORICAL_THUMBNAILS_PER_NODE = 4
HISTORICAL_INDEXES_PER_NODE = 4
RETRY_DELAYS = (1, 15, 60, 300, 3600)
PROCESSING_DISABLED = "Document processing is disabled on this deployment"


def processing_enabled() -> bool:
    return getattr(get_settings(), "document_processing_enabled", True)


def _require_processing() -> None:
    if not processing_enabled():
        raise DocumentRetry(
            503,
            PROCESSING_DISABLED,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )


class DocumentRetry(HTTPException):
    """The route must reauthorize and commit safe attempt metadata before raising."""


def _retry_later(reason: str = "Document processors busy; try again shortly") -> None:
    raise DocumentRetry(
        503,
        reason,
        headers={
            "Retry-After": "1",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _acquire_processor() -> None:
    if not _PROCESSORS.acquire(blocking=False):
        _retry_later()


_OFFICE = {
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".pptx": "pptx",
}
_OFFICE_MIME = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}
_TEXT_SUFFIXES = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".tsv",
    ".json",
    ".xml",
    ".yaml",
    ".yml",
    ".toml",
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".css",
    ".sql",
    ".log",
    ".sh",
}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def file_kind(name: str, content_type: str) -> str | None:
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    mime = content_type.lower().split(";", 1)[0].strip()
    if mime in _OFFICE_MIME:
        return _OFFICE_MIME[mime]
    if mime == "application/pdf" or suffix == ".pdf":
        return "pdf"
    if suffix in _OFFICE:
        return _OFFICE[suffix]
    if mime.startswith("image/") or suffix in _IMAGE_SUFFIXES:
        return "image"
    if (
        mime.startswith("text/")
        or mime in {"application/json", "application/xml", "application/x-yaml"}
        or suffix in _TEXT_SUFFIXES
    ):
        return "text"
    return None


def max_file_bytes() -> int:
    limit = int(getattr(get_settings(), "document_max_mb", MAX_FILE_MB))
    return max(1, min(limit, MAX_FILE_MB)) * 1024 * 1024


class OversizeBlob(ValueError):
    """The reader (not an object-store client) observed more than our limit."""


def _read_bounded(key: str, limit: int) -> bytes:
    # Don't rely on an S3 Range response being honored (or its Content-Length).
    # stream_blob closes its response body in the generator's finally block.
    chunks = []
    size = 0
    stream = storage.stream_blob(key)
    try:
        for chunk in stream:
            size += len(chunk)
            if size > limit:
                raise OversizeBlob("oversize blob")
            chunks.append(chunk)
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            close()
    return b"".join(chunks)


def _run_worker(operation: str, kind: str, data: bytes) -> tuple[str, bytes]:
    if not processing_enabled():
        return "unavailable", b""
    try:
        settings = get_settings()
        timeout = min(20.0, max(1.0, float(getattr(settings, "document_timeout_s", 20))))
        with (
            tempfile.TemporaryDirectory(prefix="xource-parser-") as cwd,
            tempfile.TemporaryFile() as source,
            document_sandbox.command(
                Path(__file__).with_name("_document_worker.py"),
                operation,
                kind,
                mode=getattr(settings, "document_parser_mode", "required"),
                is_prod=getattr(settings, "is_prod", True),
            ) as (arguments, inherited_fds),
        ):
            source.write(data)
            source.seek(0)
            with subprocess.Popen(  # noqa: S603 - fixed interpreter/script/arguments, no shell
                arguments,
                pass_fds=inherited_fds,
                stdin=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                cwd=cwd,
                # No credentials inherited; default required mode additionally
                # removes host files/PIDs/network and applies seccomp.
                env={"PATH": os.defpath, "PYTHONUTF8": "1"},
            ) as process:
                assert process.stdout is not None
                deadline = time.monotonic() + timeout
                output = bytearray()
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        while True:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0 or not selector.select(remaining):
                                return "retryable", b""  # bounded per-version parser retries
                            chunk = process.stdout.read1(64 * 1024)
                            if not chunk:
                                break
                            if len(output) + len(chunk) > MAX_WORKER_OUTPUT:
                                return "failed", b""
                            output.extend(chunk)
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
                    if process.returncode == 0:
                        return "ready", bytes(output)
                    # 3 is a known permanently invalid document; a crashed or
                    # unavailable converter may work on the next request.
                    # bwrap/bootstrap setup errors are infrastructure, never bad input.
                    result = {
                        1: "unavailable",
                        2: "unavailable",
                        3: "failed",
                        4: "unavailable",
                    }.get(process.returncode, "retryable")
                    return result, b""
                finally:
                    # Kill descendants holding stdout open, too. Parser diagnostics
                    # and unbounded capture_output buffers never reach the parent.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass  # The entire owned process group has already exited.
    except subprocess.TimeoutExpired:
        return "retryable", b""
    except (OSError, ValueError) as exc:
        log.warning("document_worker_unavailable", error=type(exc).__name__)
        return "unavailable", b""


def _outcome(data: bytes, kind: str) -> tuple[str, str | None, str, dict]:
    if kind == "image" or kind is None:
        return (
            "skipped",
            "No extractable text for this file type",
            "",
            {"kind": kind or "unsupported"},
        )
    status, output = _run_worker("extract", kind, data)
    if status in {"unavailable", "retryable"}:
        return status, "Document converter unavailable; try again shortly", "", {"kind": kind}
    if status != "ready":
        return "failed", "Document could not be parsed", "", {"kind": kind}
    try:
        preview = json.loads(output)
        if (
            not isinstance(preview, dict)
            or preview.get("kind") != kind
            or preview.get("status") != "ready"
        ):
            raise ValueError("unexpected parser result")
        text = preview.pop("text")
        preview.pop("status")
        if not isinstance(text, str) or len(text) > 250_000:
            raise ValueError("oversize parser result")
        return "ready", None, text, preview
    except (ValueError, TypeError, KeyError):
        return "failed", "Document could not be parsed", "", {"kind": kind}


def _prune_derivatives(db: Session, node: Node, model, timestamp, *, operation=None) -> None:
    filters = [model.node_id == node.id, model.version_id != node.current_version_id]
    if operation is not None:
        filters.append(model.operation == operation)
    recent = (
        select(model.version_id)
        .where(*filters)
        .order_by(timestamp.desc(), model.version_id.desc())
        .limit(HISTORICAL_INDEXES_PER_NODE)
    )
    db.execute(delete(model).where(*filters, model.version_id.not_in(recent)))


def _attempt(db: Session, node: Node, version: FileVersion, operation: str):
    marker = db.get(DocumentProcessingAttempt, (version.id, operation))
    if marker is not None and marker.node_id != node.id:
        raise ValueError("cached version does not belong to node")
    return marker


def _in_backoff(marker) -> bool:
    return marker is not None and not marker.failed and marker.next_attempt_at > datetime.now(UTC)


def _record_failure(
    db: Session, node: Node, version: FileVersion, operation: str, result: str
) -> bool:
    old = _attempt(db, node, version, operation)
    number = min((old.attempts if old else 0) + 1, 16)
    parser = int(result == "retryable")
    stmt = insert(DocumentProcessingAttempt).values(
        version_id=version.id,
        operation=operation,
        node_id=node.id,
        attempts=1,
        parser_failures=parser,
        failed=result == "failed",
        next_attempt_at=datetime.now(UTC) + timedelta(seconds=RETRY_DELAYS[min(number - 1, 4)]),
    )
    cls = DocumentProcessingAttempt
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=[cls.version_id, cls.operation],
            set_={
                "attempts": func.least(cls.attempts + 1, 16),
                "parser_failures": func.least(cls.parser_failures + parser, 3),
                "failed": cls.failed | (result == "failed") | (cls.parser_failures + parser >= 3),
                "next_attempt_at": stmt.excluded.next_attempt_at,
                "updated_at": func.now(),
            },
        )
    )
    _prune_derivatives(db, node, cls, cls.updated_at, operation=operation)
    db.flush()
    return db.get(cls, (version.id, operation), populate_existing=True).failed


def _clear_failure(db: Session, version: FileVersion, operation: str) -> None:
    db.execute(
        delete(DocumentProcessingAttempt).where(
            DocumentProcessingAttempt.version_id == version.id,
            DocumentProcessingAttempt.operation == operation,
        )
    )


def index_document(
    db: Session, node: Node, version: FileVersion, *, force: bool = False
) -> DocumentIndex:
    """Cache one immutable version. Caller authorizes first and commits.

    A failed/skipped entry is kept (no hot-loop on broken files); an editor can
    force retry. Concurrent backfill/request workers may race safely via upsert.
    """
    if node.kind != "file" or version.node_id != node.id:
        raise ValueError("version does not belong to node")
    cached = db.get(DocumentIndex, version.id)
    if cached is not None and cached.node_id != node.id:
        raise ValueError("cached version does not belong to node")
    if cached is not None and not force:
        return cached
    _require_processing()  # No attempt writes, force resets, blob reads or parser admission.
    marker = _attempt(db, node, version, "extract")
    if _in_backoff(marker):
        _retry_later("Document processing is cooling down; try again shortly")
    if force:
        _clear_failure(db, version, "extract")
        _clear_failure(db, version, "thumbnail")
    kind = file_kind(node.name, version.content_type)
    if version.size > max_file_bytes():
        outcome = (
            "skipped",
            f"File exceeds {max_file_bytes() // (1024 * 1024)} MB preview limit",
            "",
            {"kind": kind or "unsupported"},
        )
    elif kind in ("image", None):
        outcome = (
            "skipped",
            "No extractable text for this file type",
            "",
            {"kind": kind or "unsupported"},
        )
    else:
        blob = db.get(Blob, version.blob_sha256)
        if blob is None:
            _record_failure(db, node, version, "extract", "unavailable")
            _retry_later("Document bytes unavailable; try again shortly")
        _acquire_processor()
        try:
            try:
                data = _read_bounded(blob.storage_key, max_file_bytes())
            except OversizeBlob:
                # Only a stream that actually crosses the byte ceiling is oversize.
                outcome = ("skipped", "File exceeds preview limit", "", {"kind": kind})
            except Exception as exc:  # noqa: BLE001 - transient S3 failures aren't cached
                log.warning("document_blob_unavailable", error=type(exc).__name__)
                _record_failure(db, node, version, "extract", "unavailable")
                _retry_later("Document bytes unavailable; try again shortly")
            else:
                outcome = _outcome(data, kind)
        finally:
            _PROCESSORS.release()
    status, reason, text, preview = outcome
    if status in {"unavailable", "retryable"}:
        if not _record_failure(db, node, version, "extract", status):
            _retry_later(reason)
        status, reason = "failed", "Repeated parser failure; an editor can retry"
    elif status == "failed":
        _record_failure(db, node, version, "extract", "failed")
    else:
        _clear_failure(db, version, "extract")
    stmt = insert(DocumentIndex).values(
        version_id=version.id,
        node_id=node.id,
        status=status,
        reason=reason,
        text=text,
        preview=preview,
    )
    db.execute(
        stmt.on_conflict_do_update(
            index_elements=[DocumentIndex.version_id],
            set_={
                "node_id": node.id,
                "status": status,
                "reason": reason,
                "text": text,
                "preview": preview,
                "updated_at": func.now(),
            },
        )
    )
    _prune_derivatives(db, node, DocumentIndex, DocumentIndex.updated_at)
    db.flush()
    return db.get(DocumentIndex, version.id, populate_existing=True)


def thumbnail_for(version: FileVersion, name: str, blob: Blob) -> tuple[str, bytes]:
    """Return (status, PNG) for a *previously authorized* version."""
    if not processing_enabled():
        return "disabled", b""
    kind = file_kind(name, version.content_type)
    if kind not in {"pdf", "image"}:
        return "unsupported", b""
    if version.size > max_file_bytes():
        return "oversize", b""
    _acquire_processor()
    try:
        data = _read_bounded(blob.storage_key, max_file_bytes())
        return _run_worker("thumbnail", kind, data)
    except OversizeBlob:
        return "oversize", b""
    except Exception as exc:  # noqa: BLE001
        log.warning("thumbnail_blob_unavailable", error=type(exc).__name__)
        return "unavailable", b""
    finally:
        _PROCESSORS.release()


def _valid_thumbnail_png(png: bytes) -> bool:
    """Reject oversized, non-PNG or absurd raster data before caching or serving."""
    if not isinstance(png, bytes) or not 45 <= len(png) <= MAX_THUMBNAIL_BYTES:
        return False
    if png[:16] != b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
        return False
    if png[-12:] != b"\x00\x00\x00\x00IEND\xaeB`\x82":
        return False
    width, height = unpack("!II", png[16:24])
    return 0 < width <= MAX_THUMBNAIL_SIDE and 0 < height <= MAX_THUMBNAIL_SIDE


def cached_thumbnail_for(db: Session, node: Node, version: FileVersion) -> tuple[str, bytes]:
    """Return or render a thumbnail, only AFTER live scope/version authorization.

    Only PNG derivatives are stored, independent of DocumentIndex: viewing a
    thumbnail cannot mark text as indexed or suppress search backfill. Cache
    hits never touch the blob or a processor, but still require live auth.
    """
    if node.kind != "file" or version.node_id != node.id:
        raise ValueError("version does not belong to node")
    kind = file_kind(node.name, version.content_type)
    if kind not in {"pdf", "image"}:
        return "unsupported", b""
    if version.size > max_file_bytes():
        return "oversize", b""
    cached = db.get(DocumentThumbnail, version.id)
    if cached is not None:
        if cached.node_id != node.id:
            raise ValueError("cached version does not belong to node")
        if _valid_thumbnail_png(cached.png):
            return "ready", cached.png
    if not processing_enabled():
        return "disabled", b""  # Preserve even corrupt caches/attempts while disabled.
    if cached is not None:
        db.delete(cached)  # corrupt derivative, never serve it
        db.flush()
    marker = _attempt(db, node, version, "thumbnail")
    if marker is not None and marker.failed:
        return "failed", b""
    if _in_backoff(marker):
        return "unavailable", b""
    blob = db.get(Blob, version.blob_sha256)
    if blob is None:
        _record_failure(db, node, version, "thumbnail", "unavailable")
        return "unavailable", b""
    result, png = thumbnail_for(version, node.name, blob)
    if result in {"failed", "unavailable", "retryable"}:
        failed = _record_failure(db, node, version, "thumbnail", result)
        return ("failed" if failed else "unavailable"), b""
    if result != "ready":
        return result, b""
    if not _valid_thumbnail_png(png):
        failed = _record_failure(db, node, version, "thumbnail", "retryable")
        return ("failed" if failed else "unavailable"), b""
    _clear_failure(db, version, "thumbnail")
    db.execute(
        insert(DocumentThumbnail)
        .values(version_id=version.id, node_id=node.id, png=png)
        .on_conflict_do_nothing(index_elements=[DocumentThumbnail.version_id])
    )
    # Four most-recently cached historical versions plus the current version;
    # evict only replaceable derivatives, never versions/blobs/indexes/audit.
    keep_history = (
        select(DocumentThumbnail.version_id)
        .where(
            DocumentThumbnail.node_id == node.id,
            DocumentThumbnail.version_id != node.current_version_id,
        )
        .order_by(DocumentThumbnail.created_at.desc(), DocumentThumbnail.version_id.desc())
        .limit(HISTORICAL_THUMBNAILS_PER_NODE)
    )
    db.execute(
        delete(DocumentThumbnail).where(
            DocumentThumbnail.node_id == node.id,
            DocumentThumbnail.version_id != node.current_version_id,
            DocumentThumbnail.version_id.not_in(keep_history),
        )
    )
    return "ready", png


def _background_live(db, node_id, version_id) -> bool:
    node = db.get(Node, node_id)
    if node is None:
        return False
    return (
        db.scalar(
            select(Node.id)
            .join(Space)
            .where(
                Node.id == node_id,
                Node.current_version_id == version_id,
                Space.deleted_at.is_(None),
                access.untrashed_nodes([node.space_id]),
            )
        )
        is not None
    )


def index_batch(batch_size: int = 8) -> int:
    """Backfill due *current, live* versions. Poison files cannot monopolize the queue."""
    if not processing_enabled():
        return 0
    count = max(1, min(batch_size, 32))
    with SessionLocal() as db:
        candidates = db.execute(
            select(Node.id, FileVersion.id)
            .join(Space, Space.id == Node.space_id)
            .join(FileVersion, FileVersion.id == Node.current_version_id)
            .outerjoin(DocumentIndex, DocumentIndex.version_id == FileVersion.id)
            .outerjoin(
                DocumentProcessingAttempt,
                (
                    (DocumentProcessingAttempt.version_id == FileVersion.id)
                    & (DocumentProcessingAttempt.operation == "extract")
                ),
            )
            .where(
                Node.kind == "file",
                Node.deleted_at.is_(None),
                Space.deleted_at.is_(None),
                FileVersion.node_id == Node.id,
                DocumentIndex.version_id.is_(None),
                access.untrashed_nodes(select(Space.id).where(Space.deleted_at.is_(None))),
                (DocumentProcessingAttempt.version_id.is_(None))
                | (
                    (~DocumentProcessingAttempt.failed)
                    & (DocumentProcessingAttempt.next_attempt_at <= func.now())
                ),
            )
            .order_by(FileVersion.created_at.asc(), Node.id)
            .limit(count)
        ).all()
    completed = 0
    for node_id, version_id in candidates:
        try:
            with SessionLocal() as db:
                # Recheck after opening this transaction: a previous request
                # may have trashed/replaced the node while we were parsing.
                node = db.get(Node, node_id)
                if (
                    node is None
                    or node.deleted_at is not None
                    or node.current_version_id != version_id
                ):
                    continue
                space = db.get(Space, node.space_id)
                if space is None or space.deleted_at is not None:
                    continue
                version = db.get(FileVersion, version_id)
                if version is None or version.node_id != node.id:
                    continue
                if not _background_live(db, node_id, version_id):
                    continue
                try:
                    index_document(db, node, version)
                except DocumentRetry as exc:
                    if exc.detail == "Document processors busy; try again shortly":
                        return completed  # admission is not an input failure
                    db.expire_all()
                    if _background_live(db, node_id, version_id):
                        db.commit()  # backoff is durable even though no text was extracted
                    continue
                db.expire_all()
                if not _background_live(db, node_id, version_id):
                    continue
                db.commit()
                completed += 1
        except HTTPException as exc:
            if (
                exc.status_code == 503
                and exc.detail == "Document processors busy; try again shortly"
            ):
                return completed  # back off rather than hot-looping on occupied slots
            log.warning("document_index_failed", node_id=str(node_id), error=type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - continue the remaining files
            log.warning("document_index_failed", node_id=str(node_id), error=type(exc).__name__)
    return completed


async def index_forever() -> None:
    """Best-effort bounded backfill for preexisting files and new uploads.

    Parent creates/cancels this coroutine from the app lifespan. Failures never
    prevent startup; status is persisted per-file for explicit editor retry.
    """
    while True:
        if not processing_enabled() or not getattr(get_settings(), "document_index_enabled", True):
            await asyncio.sleep(30)
            continue
        batch_size = max(1, min(32, int(getattr(get_settings(), "document_index_batch", 8))))
        try:
            processed = await asyncio.to_thread(index_batch, batch_size)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - e.g. schema not migrated yet
            log.warning("document_backfill_unavailable", error=type(exc).__name__)
            processed = 0
        interval = max(1, int(getattr(get_settings(), "document_index_interval_s", 60)))
        await asyncio.sleep(0.5 if processed == batch_size else interval)
