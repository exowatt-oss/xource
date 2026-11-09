"""Same-origin guard for browser writes with cookie-based proxy authentication.

Authentication is mandatory independently of this guard. An Origin header must
match the app's configured origin or the request Host; no-Origin automated clients
must not present cross-site or same-site Sec-Fetch-Site.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi.responses import JSONResponse
from starlette.requests import Request

from app.config import Settings

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LOCAL_DEV_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:8000")


def _origin_of(url: str) -> str | None:
    """Normalize scheme + host (not path) of a configured URL."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def allowed_origins(settings: Settings) -> frozenset[str]:
    """Origins allowed to make state-changing calls, from configuration."""
    origins: set[str] = set()
    if base := _origin_of(settings.app_base_url):
        origins.add(base)
    if not settings.is_prod:
        origins.update(_LOCAL_DEV_ORIGINS)
        origins.update(o for o in map(_origin_of, settings.cors_origins) if o)
    return frozenset(origins)


def is_trusted(request: Request, allowed: frozenset[str], *, require_https: bool) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        site = request.headers.get("sec-fetch-site", "").strip().lower()
        return site not in ("cross-site", "same-site")
    origin = origin.strip().lower()
    if origin in allowed:
        return True
    parts = urlsplit(origin)
    host = request.headers.get("host", "").strip().lower()
    # Same-origin: the browser filled in both Host and Origin, and a page on
    # another site cannot make it send *our* host as its origin. The scheme
    # is checked directly rather than via X-Forwarded-Proto.
    schemes = ("https",) if require_https else ("http", "https")
    return bool(host and parts.scheme in schemes and parts.netloc.lower() == host)


def build_middleware(settings: Settings):
    allowed = allowed_origins(settings)
    require_https = settings.is_prod

    async def reject_cross_site_writes(request: Request, call_next):
        if (
            request.method in UNSAFE_METHODS
            and request.url.path.startswith("/api/")
            and not is_trusted(request, allowed, require_https=require_https)
        ):
            return JSONResponse(
                {"detail": "Cross-site request refused: this action must come from Xource itself"},
                status_code=403,
            )
        return await call_next(request)

    return reject_cross_site_writes
