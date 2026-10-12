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
- **Dual-mode AI assistant** - Quick and expert modes use separate conversations; expert mode drives a controlled
  OpenCode Runtime with tool calls, permission prompts, cancellation and diffs
- **AI file organization** - LangGraph ReAct Agent automatic classification, incremental / full organization modes, and
  inbox notifications after organization
- **All-in-one workspace** - Isolated Docker sandboxes + MCP protocol, allowing AI Agents to read/write cloud files, run
  code, and automate tasks in a Manus-style workflow
- **MCP service** - 16 tools / 4 prompts / 2 resources, directly accessible from clients such as Claude Desktop and
  Cursor, including a direct `upload_file` tool that does not shell out to curl
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
python3 scripts/build-minio-image.py --env-file .env
# Edit .env and at least configure API_URL / API_KEY / MODEL for the Chat and Embedding models
docker compose up -d --build
docker compose exec backend-api python -m entry.bootstrap_admin --username admin
```

The runtime builder verifies the locked official CLI archive checksum, uses an immutable glibc base image and Debian package snapshot, and saves its local immutable image ID into private `.env` while preserving credentials. The upstream Alpine image has a glibc incompatibility in its PTY library, so this build step is required before deployment; rebuild when moving Docker hosts. Ordinary `/tmp` remains `noexec`; the private 64 MiB `/tmp/.native` mount permits loading the PTY native library.

The upstream MinIO image is no longer pullable, so `build-minio-image.py` builds MinIO from the official source pinned in `backend/minio_image/lock.json`: it verifies the source SHA-256, fixes the Go toolchain and verifies every dependency against the upstream `go.sum`, then stores the local immutable image ID in `.env`; rebuild when moving Docker hosts.

Visit `http://app.skycloud.localhost` (default port 80) and sign in using the administrator you explicitly created. The command prompts for a password in the terminal. No administrator is automatically seeded; missing, short or placeholder signing secrets prevent startup.

The generator creates independent secrets, writes with mode 0600 and refuses to overwrite existing configuration. Startup requires independent master secrets and explicit infrastructure passwords of at least 12 characters, without defaults. Changing `.env` does not rotate passwords stored in existing PostgreSQL/RabbitMQ volumes: use trusted infrastructure administration tools to rotate those passwords, then update configuration.

The historical seeded administrator blocks startup and cannot log in. Back up the database, run migrations, then reset its password interactively from the deployment terminal:

```bash
docker compose run --rm --no-deps backend-api python -m entry.reset_admin --username admin
docker compose up -d
```

This command resets an existing administrator without creating accounts or elevating ordinary users. It preserves data and revokes prior sessions and tokens. Passwords are never passed as command-line arguments.

### Database upgrades

The current schema head is `0019_sql_cache_revisions`. API, Worker, MCP and model gateway startup only reads the Alembic revision; Compose starts them after the `migrate` service succeeds, and missing or mismatched versions prevent startup.

Back up existing databases, then run:

```bash
docker compose build
docker compose run --rm migrate
docker compose up -d
```

For host development, set `DATABASE_URL` and run `python -m entry.migrate` from `backend`. Upgrades adopt unversioned databases, preserve account and file data, repair known missing columns and vector indexes, and roll back on failure. Do not manually stamp revisions. The baseline rejects destructive downgrades; verify backup restoration before rolling back a deployment. See "Time and Time Zones" for timestamp semantics.

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

Chunk retrieval scopes both the chunk and its file to the workspace, so historical inconsistencies cannot pull another workspace's content into an answer.

## Dual-Mode AI Assistant

The assistant offers two conversation engines that never share state:

- **Quick mode**: reads the current workspace's material, reuses the RAG retrieval chain and starts no Runtime - suitable for lookups, summaries and document Q&A.
- **Expert mode**: open to `editor` / `admin` only; starts a per-user, per-workspace OpenCode Runtime with multi-step tool calls, command permission prompts, cancellation, file diffs and session recovery.

The backend entry point is `/api/assistant`; conversations, messages and runs persist in `assistant_conversations`, `assistant_messages` and `assistant_runs`. The expert Runtime uses short-lived, revocable MCP tokens and OpenCode Basic Auth; ordinary REST dependencies reject Runtime tokens, and Runtime credentials can only reach the dedicated MCP service.

Progressive rollout is controlled by `ASSISTANT_V2_ENABLED`, `ASSISTANT_EXPERT_ENABLED`, `ASSISTANT_PERMISSION_UI_ENABLED` and `ASSISTANT_RUNTIME_LOCK_TTL`. Production deployments should set a dedicated `OPENCODE_SERVER_SECRET` and use a version- or digest-pinned `OPENCODE_IMAGE`.

Switching workspaces or unmounting the assistant closes the old stream, and late responses from an old conversation cannot modify the current one. After a connection drop the frontend re-queries the server for active runs and pauses sending when the status query fails; after an initialization failure the assistant can be closed and reopened. Cancellation acknowledges the request, then the frontend keeps waiting until the run actually ends; a failed cancel keeps the original run. Permission answers apply only to the current request, no permanent allow rules are stored, and a cancelled run accepts no further permission replies.

Permission waiting and crash recovery rely on the executing process renewing its run heartbeat: another process recovering an interrupted run locks the same record and reads the latest heartbeat instead of mistaking a long task for a restart, with a default abandoned-run grace period of 45 seconds (`ASSISTANT_ABANDONED_RUN_GRACE_SECONDS`, minimum 30). A recovered run without a local SSE connection syncs status and history serially every 3 seconds and stops polling once it confirms a terminal state. Pending expert permissions persist only the permission ID, title and tool description (titles up to 2000 characters, tool descriptions up to 4000), never raw provider events; the active-run endpoint returns `pending_permission` only while that permission is still awaited and cancellation has not been requested, and the frontend matches conversation, message and permission ID when restoring, leaving only the cancel entry when information is missing.

Assistant streams use in-memory Bearer credentials and refresh only after a 401, retrying once without replaying what was already received. The parser handles split UTF-8 sequences, multi-line events and CRLF, bounds unfinished events, and releases the reader on exit or failure.

## Runtime Isolation

Runtime containers run as UID 10001 with a read-only root filesystem, all capabilities dropped, no privilege escalation, CPU/memory/PID limits, and bounded temporary storage (512 MiB workspace, 256 MiB home, 64 MiB `/tmp`). Stopping the container clears temporary data; upload files to cloud storage to retain them. Runtimes use the immutable OpenCode image ID produced by the build script and refuse to start when that local image is missing; rebuild it first.

The dedicated internal runtime network cannot reach PostgreSQL, Redis, RabbitMQ or MinIO, and has no direct Internet egress. The model gateway retains provider keys on the server. Runtimes receive revocable model-only credentials bound to their user, workspace, runtime and allowed model, valid for at most one hour. Atomic user/workspace/runtime quotas survive credential refresh and runtime recreation; failed or disconnected requests remain charged against conservative reservations. Tune quotas with `RUNTIME_LLM_HOURLY_TOKEN_BUDGET`, `RUNTIME_LLM_HOURLY_REQUEST_LIMIT` and `RUNTIME_LLM_MAX_OUTPUT_TOKENS`; the runtime's model output limit matches the gateway (default 4096) and its context budget is `RUNTIME_LLM_CONTEXT_TOKENS` (default 65536, must exceed the output limit). Provider keys are never written into Runtime configuration.

The API has neither Docker Socket access nor a runtime network interface. A separate non-root `runtime-controller` accepts only managed-container lifecycle and fixed configuration operations, enforces the pinned image and sandbox policy, and exposes no arbitrary Docker flags or exec interface. Its RPC listener binds only the private control interface and requires a separate random `RUNTIME_CONTROLLER_TOKEN`; it receives no provider or database credentials.

The Controller persists runtime container identities (generations) in `/state/generations.sqlite` on the named volume `runtime_controller_state` (mode 0700, owner UID 10002). Runtime start commits admission and a stop candidate before atomically committing the new identity and the old container's cleanup intent; historical containers are removed only through the Controller's authenticated retirement endpoint. Keep and back up that volume when upgrading, replacing or restoring the Controller - deleting the state volume loses retirement records.

Production publishes only the frontend port. Use the following to expose development services on loopback:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

For hybrid development, API/Worker can run on the host and reach the Controller at the development loopback endpoint `http://127.0.0.1:5003`. Keep MCP, the model gateway and the Controller in Compose to preserve the runtime network boundary. Runtime HTTP/session/SSE requests use the relay; runtimes publish no host port. Linux hosts must set `DOCKER_SOCKET_GID` to the socket's actual group. Generate distinct Controller and OpenCode server secrets of at least 32 characters; placeholder credentials are rejected.

Run isolated real security tests without a provider key:

```bash
cd backend
SKYCLOUD_TEST_RUNTIME_IMAGE=$(docker image inspect skycloud-opencode:1.18.11 --format '{{.Id}}') RUN_RUNTIME_SECURITY_TESTS=1 python -m pytest tests/integration/test_runtime_security.py
```

Optional model E2E tests use `OPENCODE_E2E_GATEWAY_URL`, `OPENCODE_E2E_RUNTIME_LLM_TOKEN` and `OPENCODE_E2E_MODEL`. Never provide a provider key to a Runtime.

## MCP Access

The MCP Server runs in a standalone container. Production exposes it through the frontend at **`/mcp`**; the development override publishes loopback port **5001**. Open the avatar menu → **MCP Token** to explicitly issue a credential for external clients. The full token is displayed only after issuance and is cleared when the dialog closes; it cannot be recovered. The backend stores only its SHA-256 hash and fingerprint, and GET returns metadata. The hash-storage migration revokes historical external tokens; issue and configure new credentials after upgrading. Workspaces use separate short-lived Runtime credentials, provisioned automatically on create/start/restart.

Audit records keep bounded metadata only (string lengths, set counts) and never filenames, search text, credentials or content; migration `0010_redact_mcp_audit` cleared historical argument summaries. Membership and roles are rechecked on every tool call, so caches cannot bypass a committed demotion, removal or account deletion.

| Workspace / MCP        | Token Usage                            |
|------------------------|----------------------------------------|
| ![MCP](images/mcp.png) | ![Token Usage](images/token-usage.png) |

## All-In-One Workspace

Each workspace runs in an isolated Docker container. Through the MCP protocol, AI can autonomously execute tasks inside
the sandbox and interact with the cloud drive.

| Share Management           | Workspace                          |
|----------------------------|------------------------------------|
| ![Share](images/share.png) | ![Workspace](images/workspace.png) |

## Uploads and Quotas

Upload size limits default to 256 MiB per file, 5 MiB per avatar, 512 MiB per request and 64 MiB per part. The backend counts actual request and file bytes, including bodies without Content-Length, returns 413 on overflow and removes partial temporary files. Configure positive `UPLOAD_*` values in `.env.example`; increasing the request limit also requires updating `client_max_body_size` in `frontend/nginx.conf`.

Upload quotas default to 10 GiB per user and 50 GiB per workspace, counting logical files (including instant-upload copies) plus unexpired reservations. Daily UTC admission allows 200 attempts and 5 GiB, charged when reservation succeeds; cancellation and failures do not refund the daily allowance. A user may hold 8 active uploads and a workspace 32. Reservations expire after one hour and the Worker cleans them every minute. File insertion and reservation consumption commit together, and multipart sessions are accessible only to their creator. Each API process accepts up to 8 concurrent upload requests; Nginx limits each client IP to 8 API connections and streams request bodies.

Temporary uploads use a dedicated `.staging` directory with process leases. The Worker preserves live writers and reclaims old directories after process crashes. Object keys are journaled before MinIO writes; failed or uncertain deletion keeps capacity reserved and retries on the next cleanup, so storage faults cannot bypass the quota. Interrupted merges preserve parts and a valid reservation for retry; historical files count toward capacity without retroactive daily charges.

MCP inline uploads are capped at 8 MiB, with encoded lengths checked before Base64 decoding. MCP JSON bodies default to 32 MiB and each process accepts 4 concurrent POST requests via `MCP_MAX_REQUEST_SIZE` and `MCP_REQUEST_CONCURRENCY`; increasing the body cap also requires updating Nginx `/mcp` `client_max_body_size`.

The frontend reads files in 2 MiB chunks with incremental SHA-256, pins each upload to the account, workspace and directory chosen at creation, and runs at most 3 uploads concurrently. Only transient network or server errors retry a part, and progress reaches 100% only after metadata is committed. Resume identifiers cover the file content and its context, the browser stores identifiers only, and an expired session (409) restarts initialization. Changing the directory, workspace, session or write permission discards an open file-picker selection.

## Authentication and Sessions

Browser access tokens live only in memory for fifteen minutes. Refresh credentials use a host-only HttpOnly Strict cookie (Secure and `__Host-` on HTTPS), rotate on every use, and expire absolutely after seven days. Redis stores only hash indexes and session metadata. Replaying an old refresh credential, logout, or password changes revoke access; browser Web Locks serialize cookie rotation across tabs. Refresh fails closed during Redis outages. Non-localhost deployments require an HTTPS `APP_ORIGIN`. Upgrading clears historical localStorage authentication and requires signing in again.

REST authentication accepts only `Authorization: Bearer <access-token>`; URL `?token=` parameters and generic token cookies cannot authenticate requests. File downloads and protected Markdown images fetch Blob bytes with authenticated headers, releasing image URLs on content changes or component teardown, and historical JWT query parameters are stripped before insertion. External MCP credentials also use the Authorization header.

The runtime UI uses a host-isolated HttpOnly session with a ten-minute lease. While the parent workspace is open, it redeems a new one-use ticket in a hidden iframe every five minutes and on returning to the visible page, preserving the editor. Failed renewal closes the workspace; terminal and event streams retain their own bounded leases and reconnect using the current cookie, and permission revocation does not depend on closing the page. OpenCode UI entry uses `POST /api/workspace/{id}/opencode/ui-ticket`: a one-use 30-second ticket exchanges for a session lasting at most ten minutes, and iframe URLs no longer contain Web JWTs. Each runtime requires its own hostname to isolate scripts, browser storage and service workers. The local default is `http://r{runtime_id}.skycloud.localhost:8081`; for deployment set `APP_ORIGIN` and `RUNTIME_UI_ORIGIN_TEMPLATE` (for example `https://r{runtime_id}.runtime.example.com`), configure wildcard DNS/TLS and forward these hosts to the frontend's port 8081 listener. The parent app rejects `/runtime-ui`; port 8081 forwards only the runtime gateway. Every runtime request rechecks current editor membership, password version and container identity, and streaming responses recheck credentials every 15 seconds.

Runtime ingress and the parent page must share a scheme and registrable domain for Strict SameSite cookies, for example `https://app.example.com` and `https://r{runtime_id}.runtime.example.com`. Locally open `http://app.skycloud.localhost`; for Vite use that hostname and the actual development port.

The parent CSP disallows inline scripts and eval; inline styles remain available to the component library. For custom runtime domains, set `RUNTIME_UI_CSP_SOURCE` to the corresponding CSP frame-source, such as `https://*.runtime.example.com`, matching `RUNTIME_UI_ORIGIN_TEMPLATE`. `/auth/refresh` and `/auth/logout` require same-origin POST, the exact Origin and `X-Skycloud-Session: 1` header.

## Permission Model

Workspace members hold a `viewer`, `editor` or `admin` role: reading requires membership, writing requires `editor` or `admin`, and only `admin` can change members. The owner cannot be removed or demoted, and workspace deletion is offered to the owner only.

Write paths (REST, MCP, uploads, organization, assistant and runtime) re-read and lock current membership and role inside their business transaction, and batch operations recheck item by item; stale identities held by a page, session or cache cannot bypass a committed demotion, removal or account deletion. Share creation checks current membership in the file's workspace, MCP download links bind the requesting workspace, and concurrent workspace moves reject publication.

## API and Logging Conventions

REST success responses are uniformly `{"success":true,"data":…}` and error responses are `{"success":false,"error":{"code":"…","message":"…"}}` with stable domain codes. The auth/user/workspace/file/folder/share/inbox/settings/usage/assistant JSON endpoints use that shape, while binary downloads, SSE streams, the Runtime UI proxy and the MCP/OpenAI protocols keep their own formats. Errors and logs record only fixed diagnostics, exception types and the necessary safety detail (such as field locations and types for 422), never submitted values, credentials, cache keys or raw exception text.

This shape change requires deploying frontend and backend together: the frontend strictly validates and unwraps the envelope, and an HTTP 200 without an envelope is not treated as normal business data.

## Background Jobs and Cleanup

RabbitMQ publishing uses durable messages and publisher confirms, and consumers acknowledge manually after persisting results; the semantic is at-least-once, so tasks may run twice. Invalid messages and failures that cannot persist results are copied to the durable `failed_task_queue`, which stores only the source queue, the raw message Base64 and the exception type; an administrator verifies the cause before re-publishing the original payload. Re-published organization tasks keep their original task ID, and terminal tasks are skipped.

File indexing uses a database outbox: the file record (single, batch, multipart and failed instant-upload clones) commits together with a pending task. A Worker maintenance thread checks due tasks every second, requires a RabbitMQ confirm before marking a task sent, and retries with backoff from 2 to 300 seconds, coordinating multiple Workers with row locks and SKIP LOCKED. Uploads no longer connect to RabbitMQ directly, so the Worker must run.

AI organization tasks also persist in the database: the API checks current write permission inside a transaction, at most one queued/running task exists per workspace, execution holds a workspace-level advisory lock and rotates credentials per execution so an old executor cannot keep modifying data, and terminal state commits together with the inbox notification; duplicate messages for finished tasks are skipped. Office conversions required by organization run in their own process group with a 120-second timeout that terminates the whole group and logs only a fixed failure type.

Physical object and container cleanup is asynchronous and performed by the Worker: deleting files, workspaces or accounts commits cleanup intents in the same transaction as the business data (PostgreSQL triggers plus migrations `0011`-`0014`), and the Worker performs idempotent deletes after confirming that no references remain, retrying with 2-300 second backoff. Capacity is released only after the physical object is deleted; failed or uncertain deletions stay reserved for the next retry.

## Time and Time Zones

Time API output carries an explicit UTC offset (`+00:00`); business clocks, token deadlines, assistant recovery, share expiry, runtime activity, checkpoints and usage records are stored in UTC, and shares expire exactly at their deadline. The frontend formats public dates and conversation lists in `Asia/Shanghai` and labels notification times explicitly; daily upload quotas use UTC days while usage views group by Beijing time, independent of the database session time zone.

Historical timezone-naive columns are converted to PostgreSQL TIMESTAMPTZ by migration `0009_utc_timestamps`: original Beijing columns use `Asia/Shanghai`, original upload reservations use UTC, and already-aware columns keep their instant. The migration takes table locks and may rewrite data, so stop old API/Worker/MCP/model-gateway processes, back up, and run it in a maintenance window; any failure rolls back atomically, and downgrades that drop timezone semantics are rejected.

## Local Development

```bash
# Backend (Python 3.11; requires PostgreSQL / Redis / RabbitMQ)
cd backend && python -m pip install --require-hashes --only-binary=:all: -r requirements.lock
python -m entry.api          # API
python -m entry.worker       # Worker
python -m entry.mcp_run      # MCP Server

# Frontend (Vite dev proxy -> localhost:5000)
cd frontend && npm ci && npm run dev
```

### Tests

From `backend`, install the hashed test dependencies and run the regular suite (`tests/real` and integration cases are skipped by default):

```bash
python -m pip install --require-hashes --only-binary=:all: -r requirements-test.lock
python -m pytest --ignore=tests/real
```

Integration cases that need real infrastructure are opt-in through `RUN_*` environment variables such as `RUN_MIGRATION_TESTS`, `RUN_VECTOR_TESTS`, `RUN_UPLOAD_STORAGE_TESTS`, `RUN_TASK_QUEUE_TESTS` and `RUN_RUNTIME_SECURITY_TESTS`; they create and clean up their own containers. The full list lives in `backend/tests/integration/`. From `frontend`, run `npm run test:unit`, `npm run lint:check`, `npm run type-check` and `npm run build`.

### Dependencies and CI

Runtime, test and Controller Python dependencies are locked in `backend/requirements.lock`, `backend/requirements-test.lock` and `backend/runtime_controller/requirements.lock`, covering every transitive version and the SHA-256 of released files; the inputs remain `requirements*.txt` and installs use the `.lock` files. Update dependencies with uv 0.12.1 via `python scripts/lock-python.py --upgrade`, then audit the locks, run the tests and execute `python scripts/check-python-runtime.py`. Production images (Python, Node, nginx, PostgreSQL, Redis, RabbitMQ) are pinned to amd64/arm64 index digests, and backend apt packages come from a signed Debian snapshot.

CI lives in `.github/workflows/ci.yml`: the backend installs its hashed lock and runs the suite, the frontend runs the dependency audit, lint, type checks, behavior tests and build, and an integration job runs PostgreSQL migrations plus RabbitMQ delivery/outbox/crash-recovery checks; five images (backend/controller/frontend/runtime/minio) build independently. The summary gate `CI quality gate` fails when any prerequisite fails or is skipped. Actions are pinned to verified commit SHAs and the workflow defaults to `contents:read` only. `.github/branch-protection.json` describes the master protection rule and should be applied after a successful remote run exists.

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
