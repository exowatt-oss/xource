# Xource

Self-hosted, Drive-style file storage with immutable versions, team grants, optional document previews, reviews, an audit trail and a small read-only MCP surface.

![Status: experimental](https://img.shields.io/badge/status-experimental-orange) ![License: Apache--2.0](https://img.shields.io/badge/license-Apache--2.0-blue)

Created at [Exowatt](https://exowatt.com). No Exowatt infrastructure or accounts are needed to run it.

## Status

**Experimental.** This is a scoped-down standalone server and web UI: the internal desktop sync agent, Git/repository graph, Google Drive, Slack, Windchill, people-directory sync, CI/LFS and hosted bot triggers are **not shipped**. Those integrations require a separate generic adapter and security review; the UI supports browsing, uploads, versions, raster-image previews and comments (download PDFs to view them). Native PDF/Office extraction is off by default until an operator provisions parser confinement. The first verified person to sign in becomes the administrator: configure your identity provider and private access before inviting users. Database upgrades currently need operator-managed migrations; do not point this first release at an existing internal database.

## Install / Quickstart

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 22+, npm, and PostgreSQL 16+. No hosted service is required. The `forwarded-auth` dependency must be published first; before then authenticated GitHub access to the private dependency is required.

```sh
# Example throwaway local PostgreSQL; use your own managed PostgreSQL in production.
docker run -d --name xource-postgres -p 127.0.0.1:5432:5432 \
  -e POSTGRES_USER=xource -e POSTGRES_PASSWORD=xource -e POSTGRES_DB=xource postgres:16
cp .env.example .env
cd backend && uv sync --locked
uv run python -m app.schema
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
# In a second terminal, from frontend/:
npm ci && npm run dev
```

Visit http://127.0.0.1:5173. The example's **two-step explicit local-only auth bypass** creates a synthetic developer account; it is not for shared or public deployments. The server accepts only loopback peers and loopback `Host` values in this mode, and rejects requests with proxy-forwarding headers. Run it bound to `127.0.0.1`, never behind a public proxy (a proxy that strips forwarding headers can still make remote requests appear local). Production requires a private proxy, a real JWT issuer and a persistent disk path. Change all example database credentials. For an actual release deployment, set `APP_ENV=prod`, `ENVIRONMENT=production`, remove both `FORWARDED_AUTH_MODE=none` and `FORWARDED_AUTH_ALLOW_INSECURE_NONE_MODE`, set `APP_BASE_URL=https://files.your-domain.example`, and configure `FORWARDED_AUTH_ISSUER`, `FORWARDED_AUTH_AUDIENCE` and `FORWARDED_AUTH_ALLOWED_EMAIL_DOMAINS` (e.g. your own domain). See [forwarded-auth](https://github.com/exowatt-oss/forwarded-auth) for OIDC, JWKS and trusted-proxy guidance. Production rejects none/header-only modes at startup; only a verified human may bootstrap admin. Never expose the backend port except through your configured auth proxy.

## Configuration

`DATABASE_URL` defaults to local PostgreSQL; `STORAGE_PATH` defaults to `./data/blobs` (set it to persistent disk for deployment). Blobs are SHA-256 content-addressed on local disk. Back up PostgreSQL **and** the blob directory together. Upload quotas (`SPACE_QUOTA_MB`) count all retained versions; `CLAMAV_ENABLED` opts into fail-closed malware scanning. `DOCUMENT_PROCESSING_ENABLED=false` is the default. Enable previews only after providing a supported Linux parser sandbox and confirming `DOCUMENT_PARSER_MODE=required`. The optional native parser uses namespace confinement and a short syscall blocklist, **not** a syscall allowlist; keep it disabled for untrusted files until you have assessed that boundary. Browser identity is verified with `forwarded-auth[mcp]`; the explicit `none` development mode does not verify requests. Routes under `/api` require authentication except health checks; MCP requests use the same authorization. The MCP transport's trusted hostnames can be set with `FORWARDED_AUTH_TRUSTED_HOSTNAMES`.

## Tests

Create a **different database** for tests (`docker exec xource-postgres createdb -U xource xource_test`), then run `cd backend && export TEST_DATABASE_URL=postgresql+psycopg://xource:xource@127.0.0.1:5432/xource_test && uv run ruff check --isolated --select E4,E7,E9,F src tests && uv run pytest -q`; run `cd frontend && npm ci && npm run build && npm test -- --maxWorkers=4`. The test suite refuses to start without `TEST_DATABASE_URL` naming a database ending in `_test`; it truncates that test database between cases and never touches the app database. Tests use synthetic identities and never contact Exowatt services. The test-only identity substitution is in `backend/tests/conftest.py`, never runtime code.

## Third-party notices

`psycopg` is dynamically used under LGPL-3.0-only; see [NOTICE](NOTICE). PDFium/pypdfium2 build-specific component notices are documented in [backend/THIRD_PARTY_NOTICES.md](backend/THIRD_PARTY_NOTICES.md). This tree contains no vendor SDKs, binary preview engines, brand art or font files; dependencies are downloaded by package managers.

## License

Original code is licensed under [Apache-2.0](LICENSE). See [NOTICE](NOTICE) for attributions.

## Contributing

See [CONTRIBUTING](https://github.com/exowatt-oss/.github/blob/main/CONTRIBUTING.md). Contributions require DCO sign-off (`git commit -s`); no CLA.

## Security

Report issues privately through [GitHub private vulnerability reporting](https://github.com/exowatt-oss/xource/security/advisories/new) once enabled after publication, or email software@exowatt.com. See the [security policy](https://github.com/exowatt-oss/.github/blob/main/SECURITY.md); do not open a public security issue.
