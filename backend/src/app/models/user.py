"""User model — minimal starting point.

Extend this with the columns your app needs (roles, preferences, etc.) and
add a new Alembic revision when you change it.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, String, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    # `unique=True` already creates a unique index; no separate `index=True`.
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(200))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # NOTE: ``deps.get_current_user`` bumps this on every authenticated
    # request, not only on a fresh sign-in — so the effective semantic is
    # "last seen / last active". The name is kept for consistency with
    # other platform apps (other deployments) and to avoid an
    # alembic rename. Interpret this column as "last active" in any UI /
    # dashboard surfacing it.
    last_sign_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @property
    def actor(self) -> str | None:
        """Signed machine identity acting for this user on the current request.

        This is request context from ``forwarded-auth``, not persisted user
        state. Human SSO requests return ``None``.
        """
        return getattr(self, "_request_actor", None)

    @property
    def gateway_scope(self) -> str | None:
        """Signed service grant scope for the current request, if any."""
        return getattr(self, "_request_gateway_scope", None)

    def set_gateway_context(self, *, actor: str | None, scope: str | None) -> None:
        """Attach verified, request-scoped gateway attribution."""
        self._request_actor = actor
        self._request_gateway_scope = scope
