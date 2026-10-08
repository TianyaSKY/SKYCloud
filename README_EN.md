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

Browser access tokens live only in memory for fifteen minutes. Refresh credentials use a host-only HttpOnly Strict cookie (Secure and `__Host-` on HTTPS), rotate on every use, and expire absolutely after seven days. Redis stores only hash indexes and session metadata. Replaying an old refresh credential, logout, or password changes revoke access. Browser Web Locks serialize cookie mutations across tabs. Refresh fails closed during Redis outages. Non-localhost deployments require an HTTPS `APP_ORIGIN`; upgrading clears historical localStorage authentication and requires signing in again.

The parent CSP disallows inline scripts and eval; inline styles remain available to the component library. For custom runtime domains, set `RUNTIME_UI_CSP_SOURCE` to the corresponding CSP frame-source, such as `https://*.runtime.example.com`, matching `RUNTIME_UI_ORIGIN_TEMPLATE`. `/auth/refresh` and `/auth/logout` require same-origin POST, the exact Origin and `X-Skycloud-Session: 1` header.

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
SKYCLOUD_TEST_RUNTIME_IMAGE=$(docker image inspect skycloud-opencode:1.18.11 --format '{{.Id}}') RUN_RUNTIME_SECURITY_TESTS=1 python -m pytest tests/integration/test_runtime_security.py
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

### Authentication behavior checks

From `backend`, install `python -m pip install -r requirements-test.txt` and run `python -m pytest --ignore=tests/real`. Refresh unit tests execute Lua with fakeredis. `RUN_REFRESH_SESSION_TESTS=1 python -m pytest tests/integration/test_refresh_sessions.py` verifies concurrent reuse and revocation with isolated real Redis/PostgreSQL containers, requires Docker, and removes its own resources. From `frontend`, run `npm run test:unit` and `npm run build`.


Database head is `0009_utc_timestamps`. Stop old Workers, back up the database, run `python -m entry.migrate` from backend, then start the new services. Organization requests commit a task record with fresh write permission checks; a partial unique index allows one active task per workspace. Worker maintenance publishes pending tasks with RabbitMQ confirms and retry backoff. PostgreSQL session advisory locks prevent concurrent execution; each execution rotates a token checked by tools, checkpoint writes and completion. Each running organizer holds an additional database connection. Terminal state and inbox notification commit together, and terminal replay skips model calls and notifications. Legacy JSON tasks need a valid user/task identity and cannot replace an active task. Anonymous workspace-only messages require administrator review and a new request. Old Redis locks are no longer authoritative.

Run `RUN_ORGANIZE_TASK_TESTS=1 python -m pytest -o addopts='' tests/integration/test_organize_tasks.py` in backend for real PostgreSQL/RabbitMQ admission, exclusivity, stale claims, terminal replay, broker refusal/recovery and killed-process redelivery. In-flight crashes may repeat previously committed tool changes or external model requests. Final structure validation failure records a failed task and does not advance its checkpoint.


Migration `0009_utc_timestamps` preserves legacy instants when converting timestamp columns to TIMESTAMPTZ: Beijing wall times use Asia/Shanghai; upload reservations already used UTC. Existing aware columns are not shifted again. Defaults become now(), and conversion failures roll back schema/data/defaults/version atomically. Stop old database clients (API/MCP/Worker/model gateway), back up, run migrations in a maintenance window, then start the new processes. ORM reads/writes and business clocks are UTC aware. Legacy naive request/cache inputs retain explicit Beijing semantics; old reservation inputs retain UTC semantics. Usage days are explicitly grouped in Beijing time; upload daily quotas retain UTC days. The UTC migration integration tests are included in CI.


Folder services and organizer mutations now share fresh database reads and a PostgreSQL transaction advisory lock per workspace. The canonical root cannot be renamed, moved, deleted or used as a merge source. Parent validation rejects self/descendant moves, foreign ancestry and existing cycles. Null parents and organizer root marker 0 resolve to the existing root, including file moves; REST delegates this resolution to the service and root caching uses a v2 namespace. Path serialization fails safely on cycles or foreign ancestors.

This batch passed 1607 regular backend tests with 176 opt-in skips, plus 16 actual integration checks. Three PostgreSQL checks inspect real lock waits and stale ORM identities during opposing service/tool moves and parent deletion; the others cover uploads, indexing, organization and checkpoints. Upload fixtures retain Redis/RabbitMQ seams and model providers are synthetic HTTP services. CI enables the tree tests; hosted CI remains unrun. The mutex coordinates current application writers, not raw SQL or old clients, and does not automatically repair legacy data. Schema, images and frontend are unchanged; the full original review remains incomplete.


Share, inbox and system dictionary JSON routes now use explicit typed `{success:true,data}` responses with coordinated frontend parsing. Creation retains 201; inbox/settings deletion declares 204 and anonymous share downloads retain streaming bytes and cleanup. Inbox pagination types now match pages/current_page; nullable metadata is handled explicitly. System settings request/form limits match SQL key/value/description bounds of 100/255/2048, and forms submit validated inputs separately from response metadata. Deploy frontend and backend together.

The shared share-creation service checks current membership in the file workspace for both REST and MCP, closing the missing REST authorization check. Read-authorized viewers can share; outsiders and removed members cannot. An actual PostgreSQL check confirms cached ORM membership cannot bypass a committed removal; it does not prove concurrent revocation ordering or automatically revoke existing published links. This batch passed 1616 regular backend tests with 177 opt-in skips, 258 frontend tests, type-check/lint/build and one actual PostgreSQL check. HTTP coverage includes CRUD, filtering, safe errors, downloads and OpenAPI. CI enables share access checks; hosted CI remains unrun. No schema/image changes; chat/assistant/usage/health JSON contracts and the full original review remain incomplete.


All ten personal/admin token usage JSON routes now use explicit stats, logs, pages and daily aggregation response models with coordinated frontend envelope parsing. Nullable log counters/dates match SQL metadata. Missing statistics users return 404; invalid or reversed date ranges return safe 400 errors rather than silently widening results. Valid timezone filters retain their absolute-time meaning. Deploy frontend and backend together.

This batch passed 1626 regular backend tests with 177 opt-in skips, 262 frontend tests and type-check/lint/build. Ten new HTTP checks cover endpoint variants, permissions, filtering, safe response errors and OpenAPI. Two actual PostgreSQL UTC/New York session timezone checks passed; their share fixture now seeds valid membership required by authorization. No schema/image/CI changes. Assistant JSON/health contracts and the complete original review remain open.


Assistant conversation/message/run JSON now uses explicit typed success responses, preserving data:null for absent active runs and coordinated frontend parsing. Health liveness uses a typed envelope without database access. Assistant/legacy chat SSE is explicitly documented as text/event-stream and preserves events/headers; downloads, Runtime UI, MCP and OpenAI forwarding retain their protocols. Deploy frontend and backend together.

New actual SQLite HTTP checks cover CRUD, messages, cancellation, handoff and durable permission responses, with an explicit engine seam for SSE. The full-app OpenAPI audit requires typed success/data for every public /api JSON success, allowing only explicit downloads/SSE and204 exceptions. Final regular backend1633 passed/177 opt-in skips; frontend266 and type-check/lint/build passed. Three actual PostgreSQL/Redis/Controller/OpenCode/synthetic-provider/MCP cold/allow/reject checks passed and validate persisted public DTOs; all fault variants/full browser were not rerun. This verifies public REST response contract convergence; the original service/repository/IO/error/lifecycle/browser/hostedCI scope remains incomplete. No schema/image changes.


Usage SQL writes, best-effort accounting, sync model client shutdown, reranking fallback, RAG chat and remote embedding failures now log fixed diagnostics and exception types without response bodies, exception text or attached traceback that could expose prompts/SQL binds. Rollback/close, original ranking fallback, safe SSE errors and continued client shutdown remain intact. Embedding malformed JSON/type/tensor conversion failures return None.

This batch passed 1645 regular backend tests with178 opt-in skips. Twelve diagnostic checks capture real Loguru sinks under explicit SQL/HTTPX/chain fault seams. One actual PostgreSQL rejected INSERT check confirms counter/log rollback and absence of private SQL parameters from logs. CI enables this check; hosted CI remains unrun. Frontend/schema/images are unchanged. The listed paths are covered; the full error/IO/service/repository/lifecycle/browser audit remains incomplete.


File service, MinIO failure and public500 diagnostics now omit exception bodies, object paths and traceback. Exception-bearing standard-library records emit only library name and exception type, covering Uvicorn logging after Starlette's safe500 response. Ordinary third-party messages are preserved; this is not generic log scrubbing. Download cleanup attempts release_conn even if close fails.

This batch passed1660 regular backend tests with179 opt-in skips, including15 new diagnostic/resource-release checks. One actual Uvicorn HTTP check verifies safe500/502 responses and private-free server output, plus five actual PostgreSQL/MinIO upload/rejection/crash recovery checks. Upload fixtures retain Redis/RabbitMQ seams. CI enables server diagnostics; hostedCI remainsunrun. Frontend/schema/images unchanged; organizer/worker/remaining errors and the full original review remainopen.


Office conversion now bounds the direct subprocess to120 seconds and discards stdout/stderr. PDF/video resources are released on failures, unopened captures and zero-frame returns. Description, organizer, indexing, checkpoint and worker failure logs omit raw exception traceback while preserving rollback/task/notification behavior.

This batch passed1669 regular backend tests with179 opt-in skips. Nine new checks cover actual synthetic child deadlines/stderr, explicit media cleanup/model fault seams and a real failed thread-pool future. Eight actual PostgreSQL/MinIO/Redis/MQ/lockedSDK/synthetic-provider indexing/organization checks passed; SQLcheckpoint rejection now asserts no database exception body in logs, exposing and fixing a remaining repository traceback. Actual LibreOffice descendant cleanup/native-media faults/external-provider/full-browser flows remainunverified. Frontend/schema/images unchanged; the full original review remainsincomplete.


On POSIX, Office conversion now owns a separate process session/group. Timed-out or interrupted waits terminate that group before bounded leader reaping, preserving independent conversions, discarded output and original failure status. Windows retains direct process termination without a descendant-cleanup claim.

Three actual synthetic-process checks confirm prevented late descendant writes, surviving independent processes, discarded failure output and successful completion. Focused54 tests and the full backend1672 tests passed with179 opt-in skips. Actual LibreOffice/native media/process crashes/group-escaping descendants are not claimed verified. Frontend/schema/images/CI configuration unchanged; the full original review remainsincomplete.


Actual isolated browser checks verified login, authenticated reload, chooser upload and OpenCode iframe content. Narrow-screen header/filename wrapping is fixed with a wrapping search header, horizontally scrollable table and single-line filename ellipsis; actions remain visible on narrow/no-hover views and keyboard focus. All 266 frontend checks, type-check, lint and production build passed; the rebuilt browser fixture passed. Separate HTTP checks verified small and 21 MiB multipart uploads, download SHA256, progress and instant reuse. The browser blob download event timed out, so browser download completion is unverified. Native picker, model indexing, complete expert permission recovery and external providers remain open; the original review is still in progress.


Cache failure diagnostics omit keys, exception bodies and tracebacks while preserving fallback behavior. Access/MCP signing and registration failure diagnostics are sanitized. MCP audit guards session creation, persistence, rollback and close independently so database failures do not replace tool results or original errors. The 27 new regular checks passed; full backend regression passed 1699 with 181 opt-in skips. Actual Redis ACL rejection and PostgreSQL audit INSERT rejection verified fallback, private diagnostics and subsequent audit recovery; CI includes these checks. Workspace/runtime/queue failures and the full original review remain open; external-service and process-crash guarantees are not claimed.


Runtime image/status/lifecycle/MCP configuration and member-role refresh failures now log fixed error types and numeric identities while preserving SQL states, revocation and recovery behavior. Failed RabbitMQ connection cleanup always clears handles and permits polling backoff/reconnection. Twelve new regular checks passed; the full backend suite passed 1711 with 184 opt-in skips. Actual PostgreSQL/Controller/Docker authorization rejection, production-start SQL rejection and RabbitMQ authentication rejection followed by reconnect passed. Controller tests initially retained injected invalid credentials during observation; correcting the observer credentials verified container identity preservation. Standalone Controller diagnostics and full lifecycle/browser/external-provider review remain open.


Standalone Controller policy/operation failures use fixed responses and exception types; its standard-library handler removes exception bodies, cached exception text and tracebacks. Actual testing exposed private request paths in Uvicorn access logs; access records now retain only bounded method/status, and normal HTTP-client URL logs are suppressed. Seven new checks passed; full backend regression passed 1718 with 185 opt-in skips. Rebuilt Controller image `sha256:2d0776ad1c8cf2f363e65bed9fe25dcb76b4462d8b345632a235453014ab48fe` passed six actual checks covering a synthetic Docker protocol failure, OpenCode configuration reload, network isolation and synthetic-model cold/allow/reject flows. This does not claim real daemon faults, external providers or complete browser coverage; the original review remains in progress.


Runtime configuration transforms now reside in a pure policy module; OpenCode health/config-reload/MCP HTTP operations use a separate bounded adapter. SQL lifecycle still owns transactions, fresh identity checks and token issuance, retaining existing helper entrypoints. Orchestration reduced from 806 to 696 lines. Four new configuration invariants passed; full backend regression passed 1722 with 185 opt-in skips, and three actual synthetic-provider cold/allow/reject flows passed. The locked-dependency CI smoke initially exposed a stale health-response assertion; correcting it to success/data restored actual API/MCP/organization-agent construction checks. Larger MCP/file services and broader repository/external boundaries remain open; the original review is incomplete.


MCP request authentication and tool authorization now use separate canonical modules with shared ContextVars centralized in context. Protocol helper entrypoints/tool registration remain stable; authentication sessions stay worker-owned, and real requests still exclude legacy direct-call fallback. Protocol server reduced from 1221 to 981 lines. All 112 focused checks, 1723 regular backend checks and actual locked-dependency smoke passed, with 185 opt-in skips; three actual synthetic-provider/MCP cold/allow/reject flows passed. A new concurrent-request invariant confirms role/workspace/request-ID/user isolation and restoration after error using an explicit credential-resolver seam. SQL tool units, upload and broader service/repository boundaries remain open; the original review is incomplete.


File retrieval/embedding orchestration now lives in search_service, preserving worker-owned SQL sessions, cache and shared repository/LLM adapters. Upload service retains compatibility entrypoints and reduces to 1023 lines. Backend regression passed 1723 with 187 opt-in skips; locked-dependency smoke passed. Two actual PostgreSQL/Redis checks verified workspace scope, 1024-dimensional vector ordering, warm fuzzy cache and rejection after independently committed membership removal; embedding uses an explicit vector seam. MCP and retrieval splits remain uncommitted because depleted workspace credits prevented automatic Git approval review; no Git write executed. The full original review continues.


MCP inline adaptation now uses a separate module and worker thread for encoding/MIME inference before the independently worker-owned SQL permission/upload unit. Existing 8 MiB and predecode bounds remain; protocol server reduces to 924 lines. New slow-adapter and viewer checks passed; 73 focused checks, 1725 regular checks, locked smoke and three actual synthetic-provider/MCP flows passed, with 187 opt-in skips. No full load benchmark/external-provider/native-browser claim. Changes remain uncommitted pending restoration of Git automatic approval review, previously prevented by workspace credits; no approval bypass.


MCP tools and authentication now share worker-owned SQL session cleanup. A close failure logs only its error type and attempts SQLAlchemy connection invalidation, preserving the business result or original exception; callers still own commit/rollback decisions. Six new regular fault checks passed; full backend regression: 1731 passed, 188 opt-in skips. Real PostgreSQL with an explicit Session.close fault seam verified committed data retention, uncommitted rollback, zero checked-out connections and subsequent reads. Locked API/MCP checks and three synthetic-provider cold/allow/reject scenarios passed. Native database/network faults, process crashes, external providers and full browser flows are not claimed. The original review remains in progress. Git submission remains pending following the earlier automatic approval failure due to workspace credits; last committed HEAD is 6dbf2dc.


Batch upload now includes filename generation, staging allocation and file record construction within its compensation boundary. A later preparation failure compensates objects and reservations from earlier items. Six new regular cases cover three preparation faults and deletion rejection; 91 focused checks passed, and full backend regression passed with 1737 tests and 190 opt-in skips. Two real PostgreSQL/MinIO cases inject failure of the second staging allocation: successful deletion releases capacity, while real S3 authentication rejection retains the object and capacity journal until restored access allows worker reconciliation. Daily admission attempts remain charged. Arbitrary SQL rollback/compensation commit failures, native disk faults and process crashes are not claimed. The full review continues. Git changes await restoration of the earlier automatic approval failure due to workspace credits.


Failed upload compensation now refreshes the reservation under user/workspace admission locks before deleting any object. Completed uploads remain protected; active reservation cleanup also checks file references. This prevents a successful database commit followed by an application exception from deleting saved files. Three new regular cases cover single, batch and multipart uploads; 94 focused checks passed. Full backend regression: 1740 passed, 193 opt-in skips. All 10 real PostgreSQL/MinIO upload integrations passed, including the explicit post-COMMIT fault seam preserving files, task records, completed reservations, daily charges and authenticated downloads, plus existing actual SQL rejection, S3 credential rejection/recovery and process-exit orphan cleanup. The post-COMMIT exception still returns a failed request; retry idempotency, native network disconnects and distributed atomicity are not claimed. The full review continues; Git submission awaits restoration of the earlier automatic approval failure due to workspace credits.


Upload compensation now lives in upload_recovery and is shared by single, batch, multipart, instant and initialization failure paths. Rollback failure attempts connection invalidation; one failed reservation cleanup resets the transaction and continues remaining entries without replacing the business error. Unfinished journals remain eligible for expiration reconciliation. Four new regular recovery/log privacy cases passed; 98 focused checks and 1744 full backend tests passed, with 195 opt-in skips. The final real PostgreSQL/MinIO upload suite passed all 12 cases, including actual cancellation constraint rejection preserving the original error, continuing later cleanup and releasing expired journals; a separate explicit rollback fault used actual SQLAlchemy invalidation to recover. Initial fault installation attempted DDL while an upload read transaction remained open and caused a lock wait; its test query was cancelled, and the constraint is now installed before upload. The interrupted run is not counted as successful validation. Immediate recovery from simultaneous rollback/invalidation failures, native database disconnection and retry idempotency are not claimed. The full review continues; Git changes await restoration of the prior automatic approval failure due to workspace credits.


HTTP file search and processing-status reads now inject a session factory and create, query and close their SQL sessions within the worker, instead of moving the request session into these query workers. Membership is revalidated before fuzzy cache hits and semantic/status results. Empty searches still return an empty page directly; existing authentication dependencies are outside this change. Three new thread-ownership/event-loop responsiveness checks passed; 86 focused checks and 1747 full backend tests passed, with 198 opt-in skips. Three real PostgreSQL/Redis HTTP cases retain explicit stale identity dependency snapshots and deny cached, vector and status reads after independently committed membership removal. Identity and embedding seams do not prove real JWT authentication, external providers or every authentication session boundary. Locked API/MCP checks passed. The full review continues; Git changes await restoration of the earlier automatic approval failure due to workspace credits.


HTTP avatar adaptation and session ownership now live in avatar_upload. Multipart/Base64 adapter construction, current-role SQL read, upload/share/profile updates run on the worker with its independent session. Base64 byte decoding already happened in the save worker; this change removes adapter construction from the event loop and stops forwarding the request session. Fresh role lookup rejects a demoted administrator attempting another account avatar, even with a stale role snapshot. Three new regular thread/denial checks passed; 51 focused checks and 1750 full backend tests passed, with 202 opt-in skips. Four real PostgreSQL/MinIO cases verify multipart, form Base64 and JSON Base64 account updates and shared download bytes, plus denial before object PUT after committed admin demotion behind an explicit stale identity seam. Locked API/MCP checks passed. Distributed atomicity across the multiple avatar commits, immediate termination on concurrent demotion after the role check and all HTTP authentication session ownership are not claimed. The full review continues; Git submission awaits restoration of the earlier automatic approval failure due to workspace credits.


HTTP user authentication now resolves in a dedicated authentication unit: one worker call creates its session, validates access/MCP credentials, reads the current role and cached profile, closes the session and returns a detached user snapshot. Profile caching remains; authorization role comes from a fresh SQL column read without mutating cached data. Request get_db and workspace/business dependencies remain; explicit direct-call legacy test sessions do not prove every HTTP session boundary is fixed. Two new access/MCP ownership cases passed, and final backend regression passed 1752 tests with 203 opt-in skips. Real PostgreSQL/Redis with real JWT and the admin dependency verifies demotion despite warm profile cache and subsequent auth-version revocation. All 16 actual upload integrations, three synthetic-provider/real-MCP cold/allow/reject scenarios and locked dependency checks passed. Fixtures inject the same isolated database for independent authentication sessions; constant query budgets include one fresh role read and remain independent of 1 versus 25 workspaces. The full review continues; Git submission awaits restoration of the earlier automatic approval failure due to workspace credits.


Avatar File, Share, account avatar URL, file task intent and completed reservation now publish through an internal before-commit callback in one SQL transaction. Share creation or URL update failure rolls back all publication metadata and uses existing object compensation; an exception after successful COMMIT preserves the consistent File/Share/account relationship. Profile cache is invalidated on success or error so future reads see the actual commit outcome. Avatar folder creation and quota admission/daily charging still have independent commits; database/MinIO distributed atomicity is not claimed. Three new regular fault cases passed; 68 focused checks and 1755 backend tests passed, with 206 opt-in skips. All 19 real PostgreSQL/MinIO upload integrations passed, including actual Share and account-avatar constraint rejection, and an explicit post-COMMIT fault preserving the account link, share download and task intent. Locked dependency checks passed. The full review continues; Git submission awaits restoration of the earlier automatic approval failure due to workspace credits.


Upload staging lease construction now attempts resource closure if descriptor wrapping, lease initialization, locking or extension processing fails. Directory cleanup failure still attempts handle closure. Upload services use a safe staging close entry point that logs only error type and preserves the committed result or original business error. Four new regular cases cover faults after actual OS locking, directory removal faults with closed/reacquirable lease, and close exceptions after successful/failed upload. All 43 focused checks passed; backend regression passed 1759 tests with 208 opt-in skips. Two real PostgreSQL/MinIO cases with an explicit exception after actual staging close preserve a successful 201/download, or actual SQL rejection and object compensation. Immediate release under native handle-close failures, repeated cancellation or multiple cleanup faults is not claimed. The full review continues; Git submission awaits restoration of the earlier automatic approval failure due to workspace credits.


The earlier Git automatic approval failure due to workspace credits has been resolved. The verified MCP/HTTP authentication, file read/avatar worker sessions, upload compensation/commit-outcome protection, avatar metadata transaction and staging cleanup code/tests are committed on local master as 74433bc (fix: isolate SQL workers and preserve upload recovery outcomes). The last full backend regression passed 1759 tests with 208 opt-in skips; per-batch actual integrations and explicit seams are indexed in remediation-status.json. Staged diff checks also removed trailing blank lines from four new files without changing behavior. The full original review remains in progress; no remote push was performed.
