"""Verified user mapping; no unauthenticated fallback and no trusted raw identity headers."""
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from fastapi import Depends, HTTPException, Request
from forwarded_auth import User as Identity
from forwarded_auth.fastapi import require_user
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.user import User


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    identity: Identity = require_user(request)
    if identity.is_bot:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            raise HTTPException(403, "Service identities have read-only access")
        user = User(
            id=uuid5(NAMESPACE_URL, identity.actor or identity.email),
            email=identity.email,
            name=identity.name,
            is_admin=False,
            created_at=datetime.now(UTC),
        )
    else:
        email = identity.email.lower()
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            # First *verified human* bootstraps administration. Protect the app
            # behind an identity provider before opening it to untrusted users.
            first = db.scalar(select(User.id).limit(1)) is None
            user = User(email=email, name=identity.name, is_admin=first)
            db.add(user)
            db.flush()
        else:
            user.name = identity.name
        user.last_sign_in_at = datetime.now(UTC)
        db.commit()
        db.refresh(user)
    user.set_gateway_context(actor=identity.actor, scope=identity.scope)
    return user
