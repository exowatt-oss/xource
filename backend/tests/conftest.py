"""Synthetic identities only in tests. Production never trusts caller-supplied email headers."""
import os
from collections.abc import Generator
from urllib.parse import urlsplit

# Never touch the app database: truncation is allowed only on a dedicated test DB.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
if not TEST_DATABASE_URL or not urlsplit(TEST_DATABASE_URL).path.endswith("_test"):
    raise RuntimeError("Set TEST_DATABASE_URL to a separate PostgreSQL database ending in _test")
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

os.environ.setdefault("APP_ENV", "local")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("DOCUMENT_PROCESSING_ENABLED", "false")
os.environ.setdefault("DOCUMENT_INDEX_ENABLED", "false")
os.environ.setdefault("FORWARDED_AUTH_MODE", "none")
os.environ.setdefault("FORWARDED_AUTH_ALLOW_INSECURE_NONE_MODE", "1")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from forwarded_auth import User as Identity  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import Base, engine  # noqa: E402
from app.schema import initialize  # noqa: E402
from app.main import app  # noqa: E402
from app.services import storage  # noqa: E402
import app.deps as deps  # noqa: E402
from app import models as _models  # noqa: E402,F401


@pytest.fixture(autouse=True)
def local_identity_and_storage(monkeypatch, tmp_path):
    """Only test requests with a synthetic identity header receive test users."""
    def identity(request):
        email = request.headers.get("x-test-identity")
        if not email:
            from fastapi import HTTPException
            raise HTTPException(401, "Test identity missing")
        return Identity(email=email, name=email.split("@")[0], user_id=email)

    monkeypatch.setattr(deps, "require_user", identity)
    monkeypatch.setattr(get_settings(), "storage_path", tmp_path)
    storage.ensure_bucket()


@pytest.fixture(scope="session", autouse=True)
def schema_ready():
    try:
        initialize()
    except Exception as exc:
        if os.getenv("CI"):
            pytest.fail(f"PostgreSQL required in CI: {type(exc).__name__}: {exc}")
        pytest.skip("PostgreSQL unavailable: start the local test container")


@pytest.fixture(autouse=True)
def clean_db(schema_ready) -> Generator[None, None, None]:
    try:
        with engine.begin() as conn:
            names = ", ".join('"' + table.name + '"' for table in Base.metadata.sorted_tables)
            conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    except Exception as exc:
        if os.getenv("CI"):
            pytest.fail(f"PostgreSQL required in CI: {type(exc).__name__}: {exc}")
        pytest.skip("PostgreSQL unavailable: start the local test container")
    from app.services import access_requests
    access_requests.limiter.reset()
    yield


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def auth_client(client: TestClient) -> TestClient:
    return as_user(client, "dev@example.org")


def as_user(client: TestClient, email: str) -> TestClient:
    client.headers["x-test-identity"] = email
    return client
