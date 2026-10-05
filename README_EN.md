<div align="center">
  <h1>SKYCLOUD</h1>
  <p><strong>AI-powered cloud file management platform</strong></p>
  <p>
    <img src="https://img.shields.io/badge/Python-3.10%2B-blue" alt="Python">
    <img src="https://img.shields.io/badge/Vue.js-3.x-4FC08D" alt="Vue">
    <img src="https://img.shields.io/badge/FastAPI-0.115-009688" alt="FastAPI">
    <img src="https://img.shields.io/badge/PostgreSQL-15-336791" alt="PostgreSQL">
    <img src="https://img.shields.io/badge/Docker-Enabled-2496ED" alt="Docker">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </p>
  <p><a href="./README.md">Chinese</a></p>
</div>

---

SKYCloud is an AI-enhanced cloud file management system that provides an all-in-one experience from file storage and
intelligent retrieval to autonomous execution.

## Highlights

- **File management** - Uploads (chunked uploads / instant uploads), downloads, previews, batch operations, folder
  trees, and format conversion
- **AI chat (RAG)** - Multi-dimensional keyword rewriting -> Multi-Query vector recall -> RRF fusion -> optional
  reranking -> SSE streaming output, with image references
- **AI file organization** - LangGraph ReAct Agent automatic classification, incremental / full organization modes, and
  inbox notifications after organization
- **All-in-one workspace** - Isolated Docker sandboxes + MCP protocol, allowing AI Agents to read/write cloud files, run
  code, and automate tasks in a Manus-style workflow
- **MCP service** - 17 tools / 4 prompts / 2 resources, directly accessible from clients such as Claude Desktop and
  Cursor
- **Sharing & inbox** - Expiring share links and system notification delivery
- **Token usage tracking** - Consumption statistics for chat / indexing / organization, including an admin summary view
- **Performance optimizations** - Bloom Filter permission pre-checks, Redis caching, RabbitMQ async indexing, and
  parallel embedding

| AI Assistant Chat                           | File Preview                             |
|---------------------------------------------|------------------------------------------|
| ![AI Assistant Chat](images/agent_chat.png) | ![File Preview](images/file_preview.png) |

## Architecture

![System Architecture](images/architecture.png)

| Layer           | Technology                                                                  |
|-----------------|-----------------------------------------------------------------------------|
| Backend         | FastAPI · SQLAlchemy · Pydantic                                             |
| AI              | LangChain · LangGraph · OpenAI API (compatible with SiliconFlow and others) |
| Vector Database | PostgreSQL 15 + pgvector (1024 dimensions)                                  |
| Queue / Cache   | RabbitMQ 3.13 · Redis 7                                                     |
| Frontend        | Vue 3 · TypeScript · Vite · Arco Design                                     |
| MCP             | FastMCP (Streamable HTTP)                                                   |
| Deployment      | Docker Compose                                                              |

## Quick Start

> Requirements: Python 3.11+, Docker + Docker Compose; arm64 or amd64 runtime host.

```bash
git clone https://github.com/TianyaSKY/SKYCloud && cd SKYCloud
python3 scripts/generate-env.py
python3 scripts/build-runtime-image.py --env-file .env
# Edit .env and at least configure API_URL / API_KEY / MODEL for the Chat and Embedding models
docker compose up -d --build
docker compose exec backend-api python -m entry.bootstrap_admin --username admin
```

The runtime builder verifies the locked official CLI archive checksum, uses an immutable glibc base image and Debian package snapshot, and saves its local immutable image ID into private `.env` while preserving credentials. The upstream Alpine image has a glibc incompatibility in its PTY library. Rebuild when moving Docker hosts. Ordinary `/tmp` remains `noexec`; the private 64 MiB `/tmp/.native` mount permits loading the PTY native library.

The runtime UI uses a host-isolated HttpOnly session with a ten-minute lease. While the parent workspace is open, it redeems a new one-use ticket in a hidden iframe every five minutes and on returning to the visible page, preserving the editor. Failed renewal closes the workspace. Terminal and event streams retain bounded leases and reconnect using the current cookie; permission revocation does not depend on closing the page.

Visit `http://app.skycloud.localhost` (default port 80) and sign in using the administrator you explicitly created. The command prompts for a password in the terminal. No administrator is automatically seeded; missing, short or placeholder signing secrets prevent startup.

The generator creates independent secrets, writes with mode 0600 and refuses to overwrite existing configuration. Startup requires independent master secrets and explicit infrastructure passwords of at least 12 characters, without defaults. Changing `.env` does not rotate passwords stored in existing PostgreSQL/RabbitMQ volumes: use trusted infrastructure administration tools to rotate those passwords, then update configuration.

The historical seeded administrator blocks startup and cannot log in. Back up the database, run migrations, then reset its password interactively from the deployment terminal:

```bash
docker compose run --rm --no-deps backend-api python -m entry.reset_admin --username admin
docker compose up -d
```

This command resets an existing administrator without creating accounts or elevating ordinary users. It preserves data and revokes prior sessions and tokens. Passwords are never passed as command-line arguments.

### Database upgrades

API, Worker, MCP and model gateway startup only reads the Alembic revision. Compose starts them after the `migrate` service succeeds. Missing or mismatched versions prevent startup.

Back up existing databases, then run:

```bash
docker compose build
docker compose run --rm migrate
docker compose up -d
```

For host development, set `DATABASE_URL` and run `python -m entry.migrate` from `backend`. Upgrades adopt unversioned databases, preserve account and file data, repair known missing columns and vector indexes, and roll back on failure. Do not manually stamp revisions. The baseline rejects destructive downgrades; verify backup restoration before rolling back a deployment.

Real migration tests create and clean up isolated PostgreSQL containers:

```bash
cd backend
RUN_MIGRATION_TESTS=1 python -m pytest tests/integration/test_migrations.py
```

Password changes, including administrator resets, atomically advance the target account's session version and revoke its previous Web sessions, external MCP tokens and Runtime credentials. The frontend signs out after a password change; other browsers receive 401 on their next request. Reissue and configure external MCP tokens, and restart Runtimes to obtain new credentials. Upgrading the session-version migration invalidates historical Web tokens and requires a new login.

### Stopping services (including SKYcode workspaces)

SKYcode workspaces are created dynamically with `docker run` by the backend and are **not** listed in
`docker-compose.yml`. Running `docker compose down` alone will not stop them.

Use the repo scripts to tear everything down together:

```powershell
# Windows PowerShell
.\scripts\compose-down.ps1
# Also remove compose volumes
.\scripts\compose-down.ps1 -v
```

```bash
# Linux / macOS / Git Bash
chmod +x scripts/compose-down.sh   # first time only
./scripts/compose-down.sh
./scripts/compose-down.sh -v
```

One-off manual cleanup:

```bash
docker rm -f $(docker ps -aq --filter label=skycloud.component=workspace) 2>/dev/null
docker rm -f $(docker ps -aq --filter name=skycloud-workspace-) 2>/dev/null
docker compose down
```

## RAG Retrieval Augmentation

![RAG Pipeline](images/rag_pipeline.png)

**Offline**: File upload -> RabbitMQ -> Worker generates descriptions + 1024-dimensional embeddings -> pgvector

**Online**: 6-dimensional keyword rewriting -> multi-query generation -> parallel vector recall -> RRF fusion ->
reranking -> SSE streaming output

## Runtime Isolation

Runtime containers run as UID 10001 with a read-only root filesystem, all capabilities dropped, no privilege escalation, CPU/memory/PID limits, and bounded temporary storage (512 MiB workspace, 256 MiB home, 64 MiB `/tmp`). Stopping the container clears temporary data; upload files to cloud storage to retain them.

The dedicated internal runtime network cannot reach PostgreSQL, Redis, RabbitMQ or MinIO, and has no direct Internet egress. The model gateway retains provider keys on the server. Runtimes receive revocable model-only credentials bound to their user, workspace, runtime and allowed model, valid for at most one hour. Atomic user/workspace/runtime quotas survive credential refresh and runtime recreation; failed or disconnected requests remain charged against conservative reservations.

Production publishes only the frontend port. Use `docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build` to expose development services on loopback. The API has neither Docker Socket access nor a runtime network interface. A separate non-root `runtime-controller` accepts only managed-container lifecycle and fixed configuration operations, enforces the pinned image and sandbox policy, and exposes no arbitrary Docker flags or exec interface. Its RPC listener binds only the private control interface and requires a separate random `RUNTIME_CONTROLLER_TOKEN`; it receives no provider or database credentials.

For hybrid development, API/Worker can run on the host and reach the Controller at the development loopback endpoint `http://127.0.0.1:5003`. Keep MCP, the model gateway and the Controller in Compose. Runtime HTTP/session/SSE requests use the relay; runtimes publish no host port. Linux hosts must set `DOCKER_SOCKET_GID` to the socket's actual group. Generate distinct Controller and OpenCode server secrets of at least 32 characters; placeholder credentials are rejected.

Run isolated real security tests without a provider key:

```bash
cd backend
RUN_RUNTIME_SECURITY_TESTS=1 python -m pytest tests/integration/test_runtime_security.py
```

Optional model E2E tests use `OPENCODE_E2E_GATEWAY_URL`, `OPENCODE_E2E_RUNTIME_LLM_TOKEN` and `OPENCODE_E2E_MODEL`. Never provide a provider key to a Runtime.

## MCP Access

The MCP Server runs in a standalone container. Production exposes it through the frontend at **`/mcp`**; the development override publishes loopback port **5001**. Open the avatar menu → **MCP Token** to explicitly issue a credential for external clients. The full token is displayed only after issuance and is cleared when the dialog closes; it cannot be recovered. The backend stores only its SHA-256 hash and fingerprint, and GET returns metadata. The hash-storage migration revokes historical external tokens; issue and configure new credentials after upgrading. Workspaces use separate short-lived Runtime credentials, provisioned automatically on create/start/restart.

| Workspace / MCP        | Token Usage                            |
|------------------------|----------------------------------------|
| ![MCP](images/mcp.png) | ![Token Usage](images/token-usage.png) |

## All-In-One Workspace

Each workspace runs in an isolated Docker container. Through the MCP protocol, AI can autonomously execute tasks inside
the sandbox and interact with the cloud drive.

| Share Management           | Workspace                          |
|----------------------------|------------------------------------|
| ![Share](images/share.png) | ![Workspace](images/workspace.png) |

## Local Development

```bash
# Backend (requires PostgreSQL / Redis / RabbitMQ)
cd backend && pip install -r requirements.txt
python run.py          # API
python tasks.py        # Worker
python mcp_run.py      # MCP Server

# Frontend (Vite dev proxy -> localhost:5000)
cd frontend && npm ci && npm run dev
```

## Project Structure

```
SKYCloud/
├── backend/
│   ├── app/
│   │   ├── api/            # HTTP: routers / schemas / dependencies / factory
│   │   ├── mcp/            # MCP protocol adapter (tools / resources)
│   │   ├── workers/        # Async jobs: indexing / organize / convert
│   │   ├── services/       # Shared business logic (API / MCP / Worker)
│   │   ├── models/         # SQLAlchemy models
│   │   ├── infra/          # Cache / queue / upload / datetime helpers
│   │   ├── extensions.py   # DB / Redis infrastructure
│   │   └── exceptions.py   # Shared exceptions
│   ├── run.py              # API entry point
│   ├── tasks.py            # Worker entry point
│   └── mcp_run.py          # MCP entry point
├── frontend/src/
│   ├── api/                # Axios wrappers
│   ├── components/         # Shared components
│   ├── views/              # Pages
│   ├── hooks/              # Composition API
│   └── router/             # Routes
├── docker-compose.yml
└── .env.example
```

## License

MIT

Upload size limits default to 256 MiB per file, 5 MiB per avatar, 512 MiB per request and 64 MiB per part. The backend counts actual request and file bytes, including bodies without Content-Length, returns 413 on overflow and removes partial temporary files. Configure positive `UPLOAD_*` values in `.env.example`; increasing the request limit also requires updating `client_max_body_size` in `frontend/nginx.conf`.

Upload quotas default to 10 GiB per user and 50 GiB per workspace, counting logical files (including instant-upload copies) plus unexpired reservations. Daily UTC admission allows 200 attempts and 5 GiB, charged when reservation succeeds; cancellation and failures do not refund the daily allowance. A user may hold 8 active uploads and a workspace 32. Reservations expire after one hour; the Worker cleans them every minute. File insertion and reservation consumption commit together. Multipart sessions are accessible only to their creator. Migration `0005_upload_quotas` requires reinitializing historical unfinished multipart sessions; existing files count toward capacity without retroactive daily charges. Each API process accepts up to 8 upload requests concurrently; Nginx bounds API connections to 8 per client IP and streams request bodies. Positive configuration values are listed in `.env.example`.

Temporary uploads use a dedicated `.staging` directory with process leases. The Worker preserves live writers and reclaims old directories after process crashes. Object keys are journaled before MinIO writes; the Worker deletes objects with no committed file references and acknowledges cleanup only after deletion succeeds. Failed or uncertain deletion keeps capacity reserved for later retries. Unfinished multipart sessions created before this version require reinitialization (409); historical files and references remain intact. Run real PostgreSQL/MinIO upload checks from backend with `RUN_UPLOAD_STORAGE_TESTS=1 python -m pytest tests/integration/test_upload_storage.py` (Docker required). Tests create and remove their own isolated containers; Redis caching and RabbitMQ notifications retain test substitutes.

MCP inline uploads are capped at 8 MiB, with encoded lengths checked before Base64 decoding. MCP JSON bodies default to 32 MiB and each process accepts 4 concurrent POST requests. Configure positive `MCP_MAX_REQUEST_SIZE` and `MCP_REQUEST_CONCURRENCY` values; increasing the body cap also requires updating Nginx `/mcp` client_max_body_size.

REST authentication accepts only `Authorization: Bearer <access-token>`; URL `?token=` parameters and generic token cookies cannot authenticate requests. File downloads and protected Markdown images fetch Blob bytes with authenticated headers. Image URLs are released on content changes or component teardown, and historical JWT query parameters are stripped before insertion. External MCP credentials also use the Authorization header.

OpenCode UI entry uses `POST /api/workspace/{id}/opencode/ui-ticket`: a one-use 30-second ticket exchanges for an HttpOnly runtime session lasting at most ten minutes. Iframe URLs no longer contain Web JWTs. Each runtime requires its own hostname to isolate scripts, browser storage and service workers. The local default is `http://r{runtime_id}.skycloud.localhost:8081`. For deployment set `APP_ORIGIN` and `RUNTIME_UI_ORIGIN_TEMPLATE` (for example `https://r{runtime_id}.runtime.example.com`), configure wildcard DNS/TLS and forward these hosts to the frontend's port 8081 listener. The parent app rejects `/runtime-ui`; port 8081 forwards only the runtime gateway. HTTPS uses a Secure `__Host-` cookie. Every runtime request checks current editor membership, password version and container identity. Vite development requires this separate UI ingress and `APP_ORIGIN` matching the actual development page origin.

Runtime ingress and the parent page must share a scheme and registrable domain for Strict SameSite cookies, for example `https://app.example.com` and `https://r{runtime_id}.runtime.example.com`. Locally open `http://app.skycloud.localhost`; for Vite use that hostname and the actual development port. Streaming responses recheck credentials every 15 seconds and close at expiry.
