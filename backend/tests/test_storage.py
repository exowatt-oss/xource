"""Standalone disk adapter regression checks."""
import io

import pytest

from app.services import storage


def test_content_addressed_storage_is_immutable(tmp_path):
    key = storage.put_bytes(b"first", "text/plain")[1]
    assert storage.read_blob(key) == b"first"
    assert b"".join(storage.stream_blob(key)) == b"first"
    assert key == storage.put_bytes(b"first", "text/plain")[1]
    assert storage.object_exists(key)
    assert storage.read_blob(key, 2) == b"fi"


def test_storage_rejects_path_traversal():
    for key in ("../etc/passwd", "blobs/sha256/aa/bb/" + "a" * 64,
                "blobs/sha256/../../" + "a" * 64):
        with pytest.raises(ValueError):
            storage.read_blob(key)


def test_spool_enforces_upload_limit():
    with pytest.raises(ValueError, match="limit"):
        storage.HashedSpool(io.BytesIO(b"1234"), 3)
