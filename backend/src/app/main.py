"""FastAPI application: versioned files, sharing, reviews and MCP."""
from contextlib import asynccontextmanager
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from forwarded_auth._settings import get_settings as auth_settings
from starlette.routing import Route

from app.config import get_settings
from app.csrf import build_middleware as build_csrf_middleware
from app.deps import get_current_user
from app.mcp_server import build_mcp_app
from app.models.user import User
from app.routers import access_requests, documents, explore, files, health, operations, reviews, spaces, teams
from app.services import storage


@asynccontextmanager
async def lifespan(app: FastAPI):
    auth = auth_settings()
    auth.validate()
    if get_settings().is_prod and (auth.mode == "none" or not auth.require_jwt or auth.trust_proxy_headers):
        raise RuntimeError("Production requires verified JWT authentication")
    storage.ensure_bucket()
    sub = build_mcp_app(app)
    app.state.mcp_app = sub
    async with sub.router.lifespan_context(sub):
        yield


def create_app() -> FastAPI:
    settings = get_settings()
    host = urlsplit(settings.app_base_url).hostname
    if settings.is_prod and (not host or urlsplit(settings.app_base_url).scheme != "https"):
        raise RuntimeError("Production requires an HTTPS APP_BASE_URL for host validation")
    app = FastAPI(title="Xource", description="Self-hosted versioned file storage", docs_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.middleware("http")(build_csrf_middleware(settings))

    @app.middleware("http")
    async def local_only_when_auth_disabled(request, call_next):
        if auth_settings().mode == "none":
            # Refuse local reverse proxies that could relay remote callers as
            # loopback peers while rewriting their Host to 127.0.0.1.
            if any(h in request.headers for h in ("forwarded", "x-forwarded-for", "x-forwarded-host")):
                return JSONResponse({"detail": "Insecure dev mode refuses proxy forwarding"}, status_code=403)
            peer = request.client.host if request.client else ""
            try:
                loopback = ip_address(peer).is_loopback
            except ValueError:
                loopback = peer == "testclient"  # Starlette's in-process TestClient only
            if not loopback:
                return JSONResponse({"detail": "Insecure dev mode is loopback-only"}, status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def private_responses(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["Vary"] = "Cookie, Authorization"
        return response

    @app.get("/api/auth/me")
    def me(user: User = Depends(get_current_user)):
        return {"id": str(user.id), "email": user.email, "name": user.name, "is_admin": user.is_admin}

    @app.get("/api/openapi.json", include_in_schema=False)
    def openapi(_user: User = Depends(get_current_user)):
        return JSONResponse(app.openapi())

    if not settings.is_prod:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins,
                           allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    for router in (health, operations, spaces, files, documents, reviews, access_requests, teams, explore):
        app.include_router(router.router, prefix="/api")

    class Mcp:
        async def __call__(self, scope, receive, send):
            sub = getattr(app.state, "mcp_app", None)
            if sub is None:
                return await JSONResponse({"detail": "MCP is starting"}, status_code=503)(scope, receive, send)
            return await sub(scope, receive, send)

    app.router.routes.append(Route("/mcp", endpoint=Mcp(), include_in_schema=False))
    dist = Path(__file__).resolve().parents[3] / "frontend" / "dist"
    if settings.is_prod and dist.is_dir():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="web")
    # Always check the effective Host before the CSRF same-origin comparison.
    # Dev/none mode never trusts a custom hostname, even if APP_BASE_URL is set.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[host] if settings.is_prod else ["localhost", "127.0.0.1", "testserver"])
    return app


app = create_app()
