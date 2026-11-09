"""Liveness + readiness checks."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.db import engine
from app.services import safeguards

router = APIRouter(tags=["health"])
log = structlog.get_logger("health")


@router.get("/healthz")
def healthz() -> dict:
    """Liveness probe — always 200 if the process is up."""
    return {"status": "ok"}


@router.get("/readyz")
def readyz(response: Response) -> dict:
    """Readiness requires DB and, when enabled, a responsive scanner.

    Do not return connection details to callers; scanner_status uses a bounded
    PING and reports only ready, unavailable, or disabled.
    """
    db_ok = True

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        log.warning("readyz_db_check_failed", error_type=type(exc).__name__)

    scanner = safeguards.scanner_status()
    ready = db_ok and scanner != "unavailable"
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ready" if ready else "degraded",
        "db": "ok" if db_ok else "error",
        "malware_scanner": scanner,
    }
