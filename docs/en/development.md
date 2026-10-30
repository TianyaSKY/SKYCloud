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

Test uploads use a system temporary directory that is cleaned up when the test process exits.

Integration cases that need real infrastructure are opt-in through `RUN_*` environment variables such as `RUN_MIGRATION_TESTS`, `RUN_VECTOR_TESTS`, `RUN_UPLOAD_STORAGE_TESTS`, `RUN_TASK_QUEUE_TESTS` and `RUN_RUNTIME_SECURITY_TESTS`; they create and clean up their own containers. The full list lives in `backend/tests/integration/`. From `frontend`, run `npm run test:unit`, `npm run lint:check`, `npm run type-check` and `npm run build`.

## Strict Python Static Checks

Run from the repository root in a Python 3.11 environment:

```bash
python -m pip install --require-hashes --only-binary=:all: -r backend/requirements-test.lock -r backend/requirements-quality.lock
python -m ruff check backend/app backend/entry backend/runtime_controller --config backend/ruff.toml --output-format json --exit-zero --output-file /tmp/skycloud-ruff-all.json
python scripts/check-ruff-baseline.py --report /tmp/skycloud-ruff-all.json --baseline backend/ruff-baseline.json
python -m pyright --project pyrightconfig.json
python -m mypy --config-file backend/mypy.ini
```

Pyright uses `typeCheckingMode: strict`. Ruff has no `strict` mode; `lint.select = ["ALL"]` enables all stable rules without configured ignores. Ruff automatically resolves mutually exclusive docstring rules and emits warnings. The complete Ruff report includes individually reviewed, exact baseline entries; the comparator displays every diagnostic and fails on new or stale entries. `--exit-zero` only produces the complete report and does not replace the subsequent comparison. Both checks cover production code and run in CI; the existing mypy check remains.

For cache decorators, `CacheableParams` holds the prefix, expiry and `None` policy; pass `key`, `generation_key` and `cache_allowed` as `cacheable` keyword arguments matching the decorated function's signature. The `key` callbacks of `cache_evict` and `cache_put` follow the same constraint.

## Dependencies and CI

Runtime, test and Controller Python dependencies are locked in `backend/requirements.lock`, `backend/requirements-test.lock` and `backend/runtime_controller/requirements.lock`, covering every transitive version and the SHA-256 of released files; the inputs remain `requirements*.txt` and installs use the `.lock` files. Update dependencies with uv 0.12.1 via `python scripts/lock-python.py --upgrade`, then audit the locks, run the tests and execute `python scripts/check-python-runtime.py`. Production images (Python, Node, nginx, PostgreSQL, Redis, RabbitMQ) are pinned to amd64/arm64 index digests, and backend apt packages come from a signed Debian snapshot.

Frontend dependencies are locked in `frontend/package-lock.json`; CI installs them with `npm ci` and audits all dependencies with `npm audit --audit-level=low`. When security advisories appear, run `npm audit fix` from `frontend` to update locked versions within the declared compatibility ranges, commit the lockfile, and rerun `npm ci`, the audit and the frontend checks above. Do not lower the audit threshold or skip development dependencies.

CI lives in `.github/workflows/ci.yml`: the backend installs its hashed lock and runs the suite, the frontend runs the dependency audit, lint, type checks, behavior tests and build, and the integration job checks PostgreSQL migrations, RabbitMQ delivery/outbox/crash recovery, actual OpenCode sandbox startup and configuration reload, and historical generation retirement; five images (backend/controller/frontend/runtime/minio) build independently. The summary gate `CI quality gate` fails when any prerequisite fails or is skipped. Actions are pinned to verified commit SHAs and the workflow defaults to `contents:read` only. `.github/branch-protection.json` describes the master protection rule and should be applied after a successful remote run exists.

[← Back to README](../../README_EN.md)
