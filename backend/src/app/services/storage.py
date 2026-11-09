"""Local content-addressed blob store; configure STORAGE_PATH on persistent disk.

Writing through a temp file + atomic rename keeps immutable blob paths consistent
across concurrent uploads. Never accept user-controlled storage keys.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from app.config import get_settings

CHUNK = 1024 * 1024


def root() -> Path:
    return get_settings().storage_path.resolve()


def ensure_bucket() -> None:
    root().mkdir(parents=True, exist_ok=True, mode=0o700)


def storage_key(sha256: str) -> str:
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise ValueError("Invalid SHA-256 digest")
    return f"blobs/sha256/{sha256[:2]}/{sha256[2:4]}/{sha256}"


def _path(key: str) -> Path:
    digest = key.rsplit("/", 1)[-1]
    if key != storage_key(digest):
        raise ValueError("Invalid blob key")
    return root() / key


class HashedSpool:
    def __init__(self, source: BinaryIO | None, max_bytes: int):
        self.file = tempfile.SpooledTemporaryFile(max_size=8 * CHUNK)
        self.max_bytes = max_bytes
        self._digest = hashlib.sha256()
        self.size = 0
        self.sha256 = ""
        if source is not None:
            while chunk := source.read(CHUNK):
                self.feed(chunk)
            self.finish()

    def feed(self, chunk: bytes) -> None:
        self.size += len(chunk)
        if self.size > self.max_bytes:
            self.file.close()
            raise ValueError("file exceeds upload limit")
        self._digest.update(chunk)
        self.file.write(chunk)

    def finish(self) -> None:
        self.file.seek(0)
        self.sha256 = self._digest.hexdigest()

    def close(self) -> None:
        self.file.close()


def object_exists(key: str) -> bool:
    return _path(key).is_file()


def put_blob(spool: HashedSpool, content_type: str) -> str:
    key = storage_key(spool.sha256)
    path = _path(key)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            try:
                spool.file.seek(0)
                shutil.copyfileobj(spool.file, output, CHUNK)
                output.flush()
                os.fsync(output.fileno())
                os.chmod(output.name, 0o600)
                os.replace(output.name, path)
            finally:
                if os.path.exists(output.name):
                    os.unlink(output.name)
    return key


def put_bytes(data: bytes, content_type: str) -> tuple[str, str, int]:
    import io
    spool = HashedSpool(io.BytesIO(data), len(data) + 1)
    try:
        return spool.sha256, put_blob(spool, content_type), spool.size
    finally:
        spool.close()


def stream_blob(key: str) -> Iterator[bytes]:
    with _path(key).open("rb") as file:
        while chunk := file.read(CHUNK):
            yield chunk


def read_blob(key: str, limit: int | None = None) -> bytes:
    with _path(key).open("rb") as file:
        return file.read(limit)


def copy_to(fileobj: BinaryIO, key: str) -> None:
    with _path(key).open("rb") as source:
        shutil.copyfileobj(source, fileobj, CHUNK)
