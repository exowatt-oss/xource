"""Production never uses the explicitly insecure local identity mode."""
import httpx
import pytest
from types import SimpleNamespace
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config import get_settings
from app.main import app, create_app
from app.mcp_server import call_api
from forwarded_auth._settings import Settings as AuthSettings, configure


def test_missing_identity_is_unauthorized(client):
    assert client.get('/api/spaces', headers={'X-Auth-Request-Email': 'admin@example.org'}).status_code == 401
    assert client.get('/api/openapi.json').status_code == 401


def test_real_verifier_rejects_missing_and_invalid_jwt(client, monkeypatch):
    from forwarded_auth._settings import Settings as AuthSettings, configure
    from forwarded_auth.fastapi import require_user as real_require_user
    import app.deps as deps

    config = AuthSettings(mode='forwarded', issuer='https://issuer.example.org',
                          jwks_url='https://issuer.example.org/keys', audience=('xource',),
                          allowed_email_domains=frozenset({'example.org'}))
    monkeypatch.setattr(deps, 'require_user', real_require_user)
    configure(config)
    try:
        assert client.get('/api/spaces', headers={'X-Auth-Request-Email': 'admin@example.org'}).status_code == 401
        assert client.get('/api/spaces', headers={'Authorization': 'Bearer malformed'}).status_code == 401
    finally:
        configure(None)


def test_dev_identity_rejects_proxy_forwarding(auth_client):
    assert auth_client.get('/api/spaces', headers={'X-Forwarded-For': '203.0.113.9'}).status_code == 403


def test_untrusted_host_cannot_rebind_local_dev_identity(auth_client):
    result = auth_client.post('/api/spaces', json={'name': 'Hijack'},
                              headers={'Host': 'evil.example:8000', 'Origin': 'http://evil.example:8000'})
    assert result.status_code == 400


@pytest.mark.asyncio
async def test_insecure_dev_identity_rejects_remote_peer():
    transport = httpx.ASGITransport(app=app, client=('203.0.113.1', 1234))
    async with httpx.AsyncClient(transport=transport, base_url='http://localhost') as client:
        assert (await client.get('/api/health')).status_code == 403


@pytest.mark.asyncio
async def test_mcp_inprocess_request_uses_configured_production_host(monkeypatch):
    monkeypatch.setattr(get_settings(), 'environment', 'production')
    monkeypatch.setattr(get_settings(), 'app_base_url', 'https://files.example.org')
    app_prod = create_app()
    outer = Request({'type': 'http', 'method': 'GET', 'path': '/mcp', 'query_string': b'',
                     'scheme': 'https', 'server': ('files.example.org', 443),
                     'client': ('127.0.0.1', 5000), 'headers': [(b'host', b'files.example.org')]})
    ctx = SimpleNamespace(request_context=SimpleNamespace(request=outer))
    assert await call_api(app_prod, ctx, 'GET', '/api/healthz') == {'status': 'ok'}


def test_outer_mcp_accepts_configured_production_host(monkeypatch):
    monkeypatch.setattr(get_settings(), 'environment', 'production')
    monkeypatch.setattr(get_settings(), 'app_base_url', 'https://files.example.org')
    configure(AuthSettings(mode='forwarded', issuer='https://issuer.example.org',
                           jwks_url='https://issuer.example.org/keys', audience=('xource',),
                           allowed_email_domains=frozenset({'example.org'})))
    try:
        with TestClient(create_app(), base_url='https://files.example.org') as client:
            response = client.post('/mcp', json={
                'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                'params': {'protocolVersion': '2025-06-18', 'capabilities': {},
                           'clientInfo': {'name': 'test', 'version': '0.1'}},
            }, headers={'Accept': 'application/json, text/event-stream'})
        assert response.status_code == 200, response.text[:200]
    finally:
        configure(None)


def test_insecure_mode_rejected_in_production(monkeypatch):
    monkeypatch.setattr(get_settings(), 'environment', 'production')
    monkeypatch.setattr(get_settings(), 'app_base_url', 'https://files.example.org')
    with pytest.raises(RuntimeError, match='Production requires verified JWT authentication'):
        with TestClient(create_app()):
            pass
