"""Upload safety and retained-version quota accounting.

Quota locks the space row until the request commits. Concurrent writers cannot
both spend the same remaining capacity; trash and old versions still count.
ClamAV is optional, but enabling it makes scans fail closed, never silently skip.
"""

from __future__ import annotations

import socket
import struct
import time
from typing import BinaryIO
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import FileVersion, Node, Space


def retained_bytes(db: Session, space_id: UUID) -> int:
    """Every retained version in the space."""
    versions = db.scalar(
        select(func.coalesce(func.sum(FileVersion.size), 0))
        .join(Node, FileVersion.node_id == Node.id)
        .where(Node.space_id == space_id)
    )
    return int(versions or 0)


def lock_write_space(db: Session, space_id: UUID) -> None:
    """Bound the short final write lock, independent of whether quotas are enabled."""
    try:
        db.execute(text("SET LOCAL lock_timeout = '1000ms'"))
        space = db.scalar(
            select(Space)
            .where(
                Space.id == space_id,
                Space.deleted_at.is_(None),
            )
            .with_for_update()
        )
        if space is None:
            raise HTTPException(404, "Space not found")
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) not in {"55P03", "40P01"}:
            raise
        db.rollback()
        raise HTTPException(503, "This space is busy; please retry shortly") from exc


def enforce_quota(db: Session, space_id: UUID, extra_bytes: int) -> None:
    limit = get_settings().space_quota_mb * 1024 * 1024
    if not limit:
        return
    lock_write_space(db, space_id)
    if retained_bytes(db, space_id) + extra_bytes > limit:
        raise HTTPException(413, "Space storage quota exceeded (all retained versions count)")


def _remaining(sock: socket.socket, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Scanner deadline exceeded")
    sock.settimeout(remaining)


def _reply(sock: socket.socket, deadline: float) -> bytes:
    reply = bytearray()
    while len(reply) < 4096:
        _remaining(sock, deadline)
        chunk = sock.recv(min(1024, 4096 - len(reply)))
        if not chunk:
            break
        reply.extend(chunk)
        if b"\x00" in chunk or b"\n" in chunk:
            return bytes(reply).strip(b"\x00\r\n")
    raise OSError("Incomplete scanner response")


def scan_upload(source: BinaryIO, size: int) -> str:
    """Scan a spool via clamd INSTREAM, without shell/path/file disclosure."""
    cfg = get_settings()
    if not cfg.clamav_enabled:
        return "disabled"
    if size > cfg.clamav_max_scan_mb * 1024 * 1024:
        raise HTTPException(413, "File exceeds the configured malware scan size limit")
    deadline = time.monotonic() + cfg.clamav_timeout_s
    try:
        source.seek(0)
        scanned = 0
        with socket.create_connection(
            (cfg.clamav_host, cfg.clamav_port), timeout=cfg.clamav_timeout_s
        ) as sock:
            _remaining(sock, deadline)
            sock.sendall(b"zINSTREAM\x00")
            while chunk := source.read(64 * 1024):
                scanned += len(chunk)
                if scanned > cfg.clamav_max_scan_mb * 1024 * 1024:
                    raise HTTPException(413, "File exceeds the configured malware scan size limit")
                _remaining(sock, deadline)
                sock.sendall(struct.pack("!I", len(chunk)))
                _remaining(sock, deadline)
                sock.sendall(chunk)
            _remaining(sock, deadline)
            sock.sendall(struct.pack("!I", 0))
            reply = _reply(sock, deadline)
        if reply.endswith(b" FOUND"):
            raise HTTPException(422, "Upload rejected: malware detected")
        if reply != b"stream: OK":
            raise OSError("Scanner did not confirm clean content")
        return "clean"
    except OSError as exc:
        raise HTTPException(503, "Malware scanner unavailable; upload was not stored") from exc
    finally:
        source.seek(0)


def scanner_status() -> str:
    cfg = get_settings()
    if not cfg.clamav_enabled:
        return "disabled"
    deadline = time.monotonic() + 2
    try:
        with socket.create_connection((cfg.clamav_host, cfg.clamav_port), timeout=2) as sock:
            _remaining(sock, deadline)
            sock.sendall(b"zPING\x00")
            return "ready" if _reply(sock, deadline) == b"PONG" else "unavailable"
    except OSError:
        return "unavailable"
