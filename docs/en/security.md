# Security Model

This document covers runtime isolation, authentication and sessions, the permission model, and MCP credential handling and audit.

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

## Authentication and Sessions

Browser access tokens live only in memory for fifteen minutes. Refresh credentials use a host-only HttpOnly Strict cookie (Secure and `__Host-` on HTTPS), rotate on every use, and expire absolutely after seven days. Redis stores only hash indexes and session metadata. Replaying an old refresh credential, logout, or password changes revoke access; browser Web Locks serialize cookie rotation across tabs. Refresh fails closed during Redis outages. Non-localhost deployments require an HTTPS `APP_ORIGIN`. Upgrading clears historical localStorage authentication and requires signing in again.

REST authentication accepts only `Authorization: Bearer <access-token>`; URL `?token=` parameters and generic token cookies cannot authenticate requests. File downloads and protected Markdown images fetch Blob bytes with authenticated headers, releasing image URLs on content changes or component teardown, and historical JWT query parameters are stripped before insertion. External MCP credentials also use the Authorization header.

The runtime UI uses a host-isolated HttpOnly session with a ten-minute lease. While the parent workspace is open, it redeems a new one-use ticket in a hidden iframe every five minutes and on returning to the visible page, preserving the editor. Failed renewal closes the workspace; terminal and event streams retain their own bounded leases and reconnect using the current cookie, and permission revocation does not depend on closing the page. OpenCode UI entry uses `POST /api/workspace/{id}/opencode/ui-ticket`: a one-use 30-second ticket exchanges for a session lasting at most ten minutes, and iframe URLs no longer contain Web JWTs. Each runtime requires its own hostname to isolate scripts, browser storage and service workers. The local default is `http://r{runtime_id}.skycloud.localhost:8081`; for deployment set `APP_ORIGIN` and `RUNTIME_UI_ORIGIN_TEMPLATE` (for example `https://r{runtime_id}.runtime.example.com`), configure wildcard DNS/TLS and forward these hosts to the frontend's port 8081 listener. The parent app rejects `/runtime-ui`; port 8081 forwards only the runtime gateway. Every runtime request rechecks current editor membership, password version and container identity, and streaming responses recheck credentials every 15 seconds.

Runtime ingress and the parent page must share a scheme and registrable domain for Strict SameSite cookies, for example `https://app.example.com` and `https://r{runtime_id}.runtime.example.com`. Locally open `http://app.skycloud.localhost`; for Vite use that hostname and the actual development port.

The parent CSP disallows inline scripts and eval; inline styles remain available to the component library. For custom runtime domains, set `RUNTIME_UI_CSP_SOURCE` to the corresponding CSP frame-source, such as `https://*.runtime.example.com`, matching `RUNTIME_UI_ORIGIN_TEMPLATE`. `/auth/refresh` and `/auth/logout` require same-origin POST, the exact Origin and `X-Skycloud-Session: 1` header.

## Permission Model

Workspace members hold a `viewer`, `editor` or `admin` role: reading requires membership, writing requires `editor` or `admin`, and only `admin` can change members. The owner cannot be removed or demoted, and workspace deletion is offered to the owner only.

Write paths (REST, MCP, uploads, organization, assistant and runtime) re-read and lock current membership and role inside their business transaction, and batch operations recheck item by item; stale identities held by a page, session or cache cannot bypass a committed demotion, removal or account deletion. Share creation checks current membership in the file's workspace, MCP download links bind the requesting workspace, and concurrent workspace moves reject publication.

## MCP Access: Credentials and Audit

The backend stores only the MCP token's SHA-256 hash and fingerprint, and GET returns metadata. The hash-storage migration revokes historical external tokens; issue and configure new credentials after upgrading.

Audit records keep bounded metadata only (string lengths, set counts) and never filenames, search text, credentials or content; migration `0010_redact_mcp_audit` cleared historical argument summaries. Membership and roles are rechecked on every tool call, so caches cannot bypass a committed demotion, removal or account deletion.

[← Back to README](../../README_EN.md)
