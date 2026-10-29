# Deployment and Operations

This document covers first-time deployment, administrator and secret configuration, database upgrades, password/session changes and shutting services down.

## Prerequisites and Image Builds

> Requirements: Python 3.11+, Docker + Docker Compose; arm64 or amd64 runtime host.

```bash
git clone https://github.com/TianyaSKY/SKYCloud && cd SKYCloud
python3 scripts/generate-env.py
python3 scripts/build-runtime-image.py --env-file .env
python3 scripts/build-minio-image.py --env-file .env
# Edit .env and at least configure API_URL / API_KEY / MODEL for the Chat and Embedding models (CHAT_* / EMB_*)
docker compose up -d --build
docker compose exec backend-api python -m entry.bootstrap_admin --username admin
```

The runtime builder verifies the locked official CLI archive checksum, uses an immutable glibc base image and Debian package snapshot, and saves its local immutable image ID into private `.env` while preserving credentials. The upstream Alpine image has a glibc incompatibility in its PTY library, so this build step is required before deployment; rebuild when moving Docker hosts. Ordinary `/tmp` remains `noexec`; the private 64 MiB `/tmp/.native` mount permits loading the PTY native library.

The upstream MinIO image is no longer pullable, so `build-minio-image.py` builds MinIO from the official source pinned in `backend/minio_image/lock.json`: it verifies the source SHA-256, fixes the Go toolchain and verifies every dependency against the upstream `go.sum`, then stores the local immutable image ID in `.env`; rebuild when moving Docker hosts.

## First Start and Administrator

Visit `http://app.skycloud.localhost` (default port 80) and sign in using the administrator you explicitly created. The command prompts for a password in the terminal. No administrator is automatically seeded; missing, short or placeholder signing secrets prevent startup.

The historical seeded administrator blocks startup and cannot log in. Back up the database, run migrations, then reset its password interactively from the deployment terminal:

```bash
docker compose run --rm --no-deps backend-api python -m entry.reset_admin --username admin
docker compose up -d
```

This command resets an existing administrator without creating accounts or elevating ordinary users. It preserves data and revokes prior sessions and tokens. Passwords are never passed as command-line arguments.

## Configuration and Secrets

The generator creates independent secrets, writes with mode 0600 and refuses to overwrite existing configuration. Startup requires independent master secrets and explicit infrastructure passwords of at least 12 characters, without defaults. Changing `.env` does not rotate passwords stored in existing PostgreSQL/RabbitMQ volumes: use trusted infrastructure administration tools to rotate those passwords, then update configuration.

## Database Upgrades

The current schema head is `0019_sql_cache_revisions`. API, Worker, MCP and model gateway startup only reads the Alembic revision; Compose starts them after the `migrate` service succeeds, and missing or mismatched versions prevent startup.

Back up existing databases, then run:

```bash
docker compose build
docker compose run --rm migrate
docker compose up -d
```

For host development, set `DATABASE_URL` and run `python -m entry.migrate` from `backend`. Upgrades adopt unversioned databases, preserve account and file data, repair known missing columns and vector indexes, and roll back on failure. Do not manually stamp revisions. The baseline rejects destructive downgrades; verify backup restoration before rolling back a deployment. See [time and time zones](operations.md) for timestamp semantics.

Real migration tests create and clean up isolated PostgreSQL containers:

```bash
cd backend
RUN_MIGRATION_TESTS=1 python -m pytest tests/integration/test_migrations.py
```

## Password and Session Changes

Password changes, including administrator resets, atomically advance the target account's session version and revoke its previous Web sessions, external MCP tokens and Runtime credentials. The frontend signs out after a password change; other browsers receive 401 on their next request. Reissue and configure external MCP tokens, and restart Runtimes to obtain new credentials. Upgrading the session-version migration invalidates historical Web tokens and requires a new login.

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
