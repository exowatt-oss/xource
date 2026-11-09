"""Admin-only operational readback; never claims a backup/scan is healthy unseen."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.models import Space, User
from app.services import document_runtime, safeguards

router = APIRouter(prefix="/operations", tags=["operations"])


@router.get("/status")
def status(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if not user.is_admin or user.actor:
        raise HTTPException(403, "Xource admin access required")
    cfg = get_settings()
    limit = cfg.space_quota_mb * 1024 * 1024
    return {
        "malware_scanner": safeguards.scanner_status(),
        "scan_max_bytes": cfg.clamav_max_scan_mb * 1024 * 1024,
        "upload_max_bytes": cfg.max_upload_mb * 1024 * 1024,
        "space_quota_bytes": limit or None,
        "preview_max_bytes": cfg.document_max_mb * 1024 * 1024,
        "document_processing": "enabled" if cfg.document_processing_enabled else "disabled",
        "document_sandbox": document_runtime.sandbox_status(),
        "backup_status": "not_verified",
        "backup_note": (
            "Off-host scheduling and production restore verification require an operator."
        ),
        "spaces": [
            {
                "id": str(s.id),
                "name": s.name,
                "deleted": s.deleted_at is not None,
                "retained_bytes": safeguards.retained_bytes(db, s.id),
                "quota_bytes": limit or None,
            }
            for s in db.scalars(select(Space).order_by(Space.name)).all()
        ],
    }
