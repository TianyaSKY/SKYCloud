# Deployment and Operations

This document covers first-time deployment, administrator and secret configuration, database upgrades, password/session changes and shutting services down.

## Prerequisites and Image Builds

> Requirements: uv 0.13.0 (Python 3.11), Docker + Docker Compose; arm64 or amd64 runtime host.

Python dependencies live in one place: `pyproject.toml` declares them and `uv.lock` locks them, and host scripts need only the standard library and run through `uv run --no-project python`. Backend and Controller images install with `uv sync --locked --no-dev` and `uv sync --locked --no-default-groups --only-group controller` respectively; there are no separate requirements lockfiles.

```bash
git clone https://github.com/TianyaSKY/SKYCloud && cd SKYCloud
cp .env.example .env
# Edit .env and configure the 6 model parameters for Chat and Embedding (CHAT_* / EMB_*)
uv run --no-project python scripts/start.py
docker compose exec backend-api python -m entry.bootstrap_admin --username admin
```

The unified startup script `scripts/start.py` automatically performs:
1. Secret initialization (persisted to `.skycloud/generated.env` with private `0600` permissions; existing secrets are never overwritten);
2. Verifies and builds the Runtime sandbox and MinIO images, recording immutable image IDs into `.skycloud/generated.env`;
3. Executes `docker compose up -d --build` (supports `--dev` to expose host ports, or `--check-only` for preflight checks).

When moving Docker hosts or updating system images, run `uv run --no-project python scripts/start.py --rebuild-images`.

If upgrading from an earlier version with 86 environment variables, run the migration script:
```bash
uv run --no-project python scripts/migrate-env.py
```
This backs up the legacy `.env` to `.env.bak`, migrates existing system secrets to `.skycloud/generated.env`, preserves the 6 model parameters in `.env`, and routes non-default overrides into `.env.advanced`. Database credentials and data volumes remain completely intact.

The runtime builder verifies the locked official CLI archive checksum, uses an immutable glibc base image and Debian package snapshot, and saves its local immutable image ID into `.skycloud/generated.env` while preserving credentials. The upstream Alpine image has a glibc incompatibility in its PTY library, so this build step is required before deployment; rebuild when moving Docker hosts. Ordinary `/tmp` remains `noexec`; the private 64 MiB `/tmp/.native` mount permits loading the PTY native library.

The upstream MinIO image is no longer pullable, so `build-minio-image.py` builds MinIO from the official source pinned in `backend/minio_image/lock.json`: it verifies the source SHA-256, fixes the Go toolchain and verifies every dependency against the upstream `go.sum`, then stores the local immutable image ID in `.skycloud/generated.env`; rebuild when moving Docker hosts.

## First Start and Administrator

Visit `http://app.skycloud.localhost` (default port 80) and sign in using the administrator you explicitly created. The command prompts for a password in the terminal. No administrator is automatically seeded; missing, short or placeholder signing secrets prevent startup.

The historical seeded administrator blocks startup and cannot log in. Back up the database, confirm its schema version is compatible, then reset its password interactively from the deployment terminal:

```bash
docker compose run --rm --no-deps backend-api python -m entry.reset_admin --username admin
docker compose up -d
```

This command resets an existing administrator without creating accounts or elevating ordinary users. It preserves data and revokes prior sessions and tokens. Passwords are never passed as command-line arguments.

## Configuration and Secrets

SKYCloud uses a layered configuration architecture:
1. **User Model Configuration (`.env`)**: Contains only the 6 model parameters for Chat and Embedding models (API_URL, API_KEY, and MODEL names).
2. **System Private Credentials (`.skycloud/generated.env`)**: Automatically generated on first start (passwords for PostgreSQL, RabbitMQ, and MinIO, keys for JWT, OpenCode, and Runtime Controller, plus built image IDs). Persisted with private `0600` permissions and never tracked by Git or modified by service restarts.
3. **Optional Advanced Settings (`.env.advanced`)**: Override host ports, domains, TLS/HTTPS, worker thread pools, or storage quotas by creating `.env.advanced` from `.env.advanced.example`.

Startup requires independent master secrets and explicit infrastructure passwords of at least 12 characters, without defaults. Changing passwords does not automatically update existing PostgreSQL or RabbitMQ volumes: use trusted infrastructure management tools to rotate volume credentials before synchronizing `.skycloud/generated.env`.

## Database Initialization and Rebuild

The `init-db` service initializes an empty PostgreSQL database using SQLAlchemy models, installs the pgvector extension and PostgreSQL functions and triggers, and inserts the initial site configuration in one transaction. A transaction-level advisory lock serializes concurrent initialization.

API, Worker, MCP and model gateway startup only checks the schema version; it never creates tables. For compatibility with existing databases, the `0001_rebuild` marker remains in `alembic_version`, but Alembic is no longer required. Databases with that version are left untouched; missing or mismatched versions prevent startup.

For a new deployment or an explicitly authorized full rebuild, configure an isolated empty database first:

```bash
docker compose build
docker compose run --rm init-db
docker compose up -d
```

For host development, set `DATABASE_URL` and `SECRET_KEY` (the system secret in `.skycloud/generated.env` can be used), then run `uv run --locked python -m entry.init_db` from `backend`. Other versions and any unversioned tables are rejected; there is no old-schema upgrade, column repair, or historical business-data conversion. SQLAlchemy `create_all()` does not upgrade existing tables; future schema changes require an explicit upgrade plan. Before switching an existing deployment, stop every old writer and verify backup restoration; rebuild the database and associated files/objects only within the explicitly authorized scope, preserving deployment configuration and external credentials separately. Rollback requires a verified full backup.

Real initialization tests create and clean up isolated PostgreSQL containers:

```bash
RUN_DATABASE_INIT_TESTS=1 uv run --locked pytest backend/tests/integration/test_database_init.py
```

## Password and Session Changes

Password changes, including administrator resets, atomically advance the target account's session version and revoke its previous Web sessions, external MCP tokens and Runtime credentials. The frontend signs out after a password change; other browsers receive 401 on their next request. Reissue and configure external MCP tokens, and restart Runtimes to obtain new credentials. Enabling session-version checks invalidates historical Web tokens and requires a new login.

## Stopping Services (including SKYcode workspaces)

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

[← Back to README](../../README_EN.md)
