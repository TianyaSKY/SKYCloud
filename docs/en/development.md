# Local Development

This document covers running the backend and frontend locally, the test suites, and dependency locking and CI.

## Running Locally

```bash
# Backend (Python 3.11; requires PostgreSQL / Redis / RabbitMQ)
cd backend && python -m pip install --require-hashes --only-binary=:all: -r requirements.lock
python -m entry.api          # API
python -m entry.worker       # Worker
python -m entry.mcp_run      # MCP Server

# Frontend (Vite dev proxy -> localhost:5000)
cd frontend && npm ci && npm run dev
```

## Tests

From `backend`, install the hashed test dependencies and run the regular suite (`tests/real` and integration cases are skipped by default):

```bash
python -m pip install --require-hashes --only-binary=:all: -r requirements-test.lock
python -m pytest --ignore=tests/real
```

Integration cases that need real infrastructure are opt-in through `RUN_*` environment variables such as `RUN_MIGRATION_TESTS`, `RUN_VECTOR_TESTS`, `RUN_UPLOAD_STORAGE_TESTS`, `RUN_TASK_QUEUE_TESTS` and `RUN_RUNTIME_SECURITY_TESTS`; they create and clean up their own containers. The full list lives in `backend/tests/integration/`. From `frontend`, run `npm run test:unit`, `npm run lint:check`, `npm run type-check` and `npm run build`.

## Dependencies and CI

Runtime, test and Controller Python dependencies are locked in `backend/requirements.lock`, `backend/requirements-test.lock` and `backend/runtime_controller/requirements.lock`, covering every transitive version and the SHA-256 of released files; the inputs remain `requirements*.txt` and installs use the `.lock` files. Update dependencies with uv 0.12.1 via `python scripts/lock-python.py --upgrade`, then audit the locks, run the tests and execute `python scripts/check-python-runtime.py`. Production images (Python, Node, nginx, PostgreSQL, Redis, RabbitMQ) are pinned to amd64/arm64 index digests, and backend apt packages come from a signed Debian snapshot.

CI lives in `.github/workflows/ci.yml`: the backend installs its hashed lock and runs the suite, the frontend runs the dependency audit, lint, type checks, behavior tests and build, and an integration job runs PostgreSQL migrations plus RabbitMQ delivery/outbox/crash-recovery checks; five images (backend/controller/frontend/runtime/minio) build independently. The summary gate `CI quality gate` fails when any prerequisite fails or is skipped. Actions are pinned to verified commit SHAs and the workflow defaults to `contents:read` only. `.github/branch-protection.json` describes the master protection rule and should be applied after a successful remote run exists.

[← Back to README](../../README_EN.md)
