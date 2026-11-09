"""Authenticated MCP facade over the same REST authorization checks as the web UI."""
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from forwarded_auth.mcp import build_transport_security
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from starlette.types import ASGIApp
from app.config import get_settings
from app.services import audit


def app_origin() -> str:
    """Return the configured origin only; APP_BASE_URL must not supply an API path."""
    configured = get_settings().app_base_url or "http://localhost"
    parsed = urlsplit(configured)
    return f"{parsed.scheme}://{parsed.netloc}"


async def call_api(api: ASGIApp, ctx: Context, method: str, path: str, **kwargs):
    """In-process API call, retaining identity and the configured public Host."""
    outer = ctx.request_context.request
    if outer is None:
        raise ToolError("An authenticated HTTP connection is required")
    headers = {k: v for k, v in outer.headers.items() if k.lower() not in
               {"host", "content-length", "content-type", "mcp-session-id", "mcp-protocol-version", audit.VIA_HEADER}}
    headers[audit.VIA_HEADER] = f"{audit.VIA_TOKEN} mcp"
    # ASGITransport does not dial this URL: it supplies the production Host.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                 base_url=app_origin(), timeout=60) as client:
        response = await client.request(method, path, headers=headers, **kwargs)
    if response.status_code >= 400:
        raise ToolError(f"{response.status_code}: {response.text[:400]}")
    return response.json()


def build_mcp_app(api: ASGIApp) -> ASGIApp:
    origin = app_origin()
    host = urlsplit(origin).hostname
    mcp = FastMCP("xource", instructions="Self-hosted versioned file storage. Start with list_spaces.",
                  streamable_http_path="/mcp", transport_security=build_transport_security(
                      extra_allowed_hosts=[host, f"{host}:*"] if host else [],
                      extra_allowed_origins=[origin],
                  ))
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    async def call(ctx: Context, method: str, path: str, **kwargs):
        return await call_api(api, ctx, method, path, **kwargs)

    @mcp.tool(annotations=read)
    async def list_spaces(ctx: Context) -> list[dict]:
        """List spaces visible to your identity and its role on each."""
        return await call(ctx, "GET", "/api/spaces")

    @mcp.tool(annotations=read)
    async def list_children(ctx: Context, space_id: UUID, parent_id: UUID | None = None) -> dict:
        """List folders and files at a given location."""
        return await call(ctx, "GET", f"/api/spaces/{space_id}/children",
                          params={"parent_id": str(parent_id)} if parent_id else {})

    @mcp.tool(annotations=read)
    async def list_versions(ctx: Context, node_id: UUID) -> list[dict]:
        """List immutable versions of a file."""
        return await call(ctx, "GET", f"/api/nodes/{node_id}/versions")

    @mcp.tool(annotations=read)
    async def list_comments(ctx: Context, node_id: UUID) -> list[dict]:
        """List version-specific review comments on a file."""
        return await call(ctx, "GET", f"/api/nodes/{node_id}/comments")

    @mcp.tool(annotations=read)
    async def search_files(ctx: Context, query: str) -> list[dict]:
        """Find accessible files by name or indexed text."""
        return await call(ctx, "GET", "/api/search", params={"q": query})

    return mcp.streamable_http_app()
