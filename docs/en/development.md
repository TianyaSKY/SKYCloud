# Local Development

This document covers running the backend and frontend locally, the test suites, and dependency management and CI.

Python dependencies are declared in the root `pyproject.toml` and locked by the `uv.lock` beside it; uv 0.13.0 and Python 3.11 are required. The default `dev` group includes `test` and `quality`; `uv run --locked` reuses that environment and fails when `uv.lock` is out of sync with `pyproject.toml`. Commands below run from the repository root unless noted otherwise.

## Running Locally

```bash
uv sync --locked   # create .venv and install the dev dependencies

# Backend (requires PostgreSQL / Redis / RabbitMQ); entry points run with backend as the working directory
cd backend
uv run --locked python -m entry.api          # API
uv run --locked python -m entry.worker       # Worker
uv run --locked python -m entry.mcp_run      # MCP Server

# Frontend (Vite dev proxy -> localhost:5000)
cd frontend && npm ci && npm run dev
```

## Tests

Test settings live in `[tool.pytest.ini_options]` in the root `pyproject.toml`: `testpaths = ["backend/tests"]` and `pythonpath = ["backend"]`, so the suite runs from either the repository root or `backend`. `tests/real` and integration cases are skipped by default.

```bash
PYTHON_DOTENV_DISABLED=1 uv run --locked pytest --ignore=backend/tests/real
```

Tests must not read the workstation `.env` or depend on real provider credentials; gateway fixtures explicitly set synthetic `CHAT_API_URL`, `CHAT_API_KEY` and `CHAT_API_MODEL` values. When diagnosing CI differences, also clear exported workstation variables, for example by running the command in an `env -i` environment retaining only `HOME` and `PATH`.

Test uploads use a system temporary directory that is cleaned up when the test process exits.

Integration cases that need real infrastructure are opt-in through `RUN_*` environment variables such as `RUN_DATABASE_INIT_TESTS`, `RUN_VECTOR_TESTS`, `RUN_UPLOAD_STORAGE_TESTS`, `RUN_TASK_QUEUE_TESTS` and `RUN_RUNTIME_SECURITY_TESTS`; they create and clean up their own containers. The full list lives in `backend/tests/integration/`. From `frontend`, run `npm run test:unit`, `npm run lint:check`, `npm run type-check` and `npm run build`.

Runtime hot refresh rotates MCP credentials and revokes old tokens; subsequent MCP calls must re-read the live runtime configuration instead of reusing pre-refresh headers. When running Docker integration cases on a workstation with proxy variables set, unset `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` and their lowercase variants for that test process so local fixture requests do not use the workstation proxy.

## Strict Python Static Checks

Run from the repository root. Ruff, Pyright and Mypy are configured entirely in the root `pyproject.toml` (`[tool.ruff]`, `[tool.pyright]`, `[tool.mypy]`); there are no separate configuration files and the commands point at that file directly:

```bash
uv run --locked ruff check backend/app backend/entry backend/runtime_controller backend/runtime_common --output-format json --exit-zero --output-file /tmp/skycloud-ruff-all.json
uv run --locked python scripts/check_ruff_baseline.py --report /tmp/skycloud-ruff-all.json --baseline backend/ruff-baseline.json
uv run --locked pyright --project pyproject.toml
uv run --locked mypy --config-file pyproject.toml
```

Pyright uses `typeCheckingMode: strict`. Ruff has no `strict` mode; `lint.select = ["ALL"]` enables all stable rules without configured ignores. Ruff automatically resolves mutually exclusive docstring rules and emits warnings. The complete Ruff report includes individually reviewed, exact baseline entries; the comparator displays every diagnostic and fails on new or stale entries. `--exit-zero` only produces the complete report and does not replace the subsequent comparison. Both checks cover production code and run in CI; the existing mypy check remains.

Fix ordinary lint violations, such as `I001` import ordering, in the source file rather than expanding the baseline.

`cacheable` is the only cache-writing decorator: it reads Redis first and only runs the function on a miss. Invalidation comes from key callbacks instead of deleting unsuffixed keys: `key` and `generation_key` bind entries to a SQL revision or a rotated namespace, and `cache_allowed` rejects transactions whose writes may roll back. Pass them as `cacheable` keyword arguments matching the decorated function's signature. `CacheableParams` holds the prefix, expiry and `None` policy.

## Dependencies and CI

There is a single Python lockfile: the root `uv.lock`. `[project].dependencies` holds the runtime dependencies; `test`, `quality` and `controller` are dependency groups, and the default `dev` group includes `test` and `quality`. The Controller service installs its own group without pulling in the root runtime dependencies:

```bash
uv sync --locked --no-default-groups --only-group controller
```

Production images install runtime dependencies with `uv sync --locked --no-dev`, and the Controller image uses `uv sync --locked --no-default-groups --only-group controller`; CI runs `uv sync --locked` (the integration job uses `uv sync --locked --no-default-groups --group test`) before the checks. To update dependencies, run `uv lock --upgrade`, review `uv.lock`, then `uv sync --locked`, run the tests and execute `uv run --locked python scripts/check-python-runtime.py`. The uv version is pinned to 0.13.0 by `[tool.uv] required-version` and Python to 3.11 by `requires-python = ">=3.11,<3.12"`. Production images (Python, Node, nginx, PostgreSQL, Redis, RabbitMQ) are pinned to amd64/arm64 index digests, and backend apt packages come from a signed Debian snapshot.

Frontend dependencies remain locked in `frontend/package-lock.json`, independent of the uv project; CI installs them with `npm ci` and audits all dependencies with `npm audit --audit-level=low`. When security advisories appear, run `npm audit fix` from `frontend` to update locked versions within the declared compatibility ranges, commit the lockfile, and rerun `npm ci`, the audit and the frontend checks above. Do not lower the audit threshold or skip development dependencies.

`frontend/package.json` uses scoped `overrides`: `js-beautify` receives `glob ^14.0.4`, while `editorconfig` and `@typescript-eslint/typescript-estree` receive `minimatch ^11.0.5`, fixing traversal-cancellation and pattern-amplification denial-of-service vulnerabilities. Upstream dependency ranges do not yet include the patched majors, so `npm audit fix` alone cannot resolve these advisories; do not use `--force` to downgrade the TypeScript tooling. Remove each override when its upstream dependency range includes a patched version, then rerun all frontend checks.

CI lives in `.github/workflows/ci.yml`: the backend installs uv 0.13.0 and its locked dependencies with `uv sync --locked` and runs the suite, the frontend runs the dependency audit, lint, type checks, behavior tests and build, and the integration job checks PostgreSQL initialization, RabbitMQ delivery/outbox/crash recovery, actual OpenCode sandbox startup and configuration reload, and historical generation retirement; five images (backend/controller/frontend/runtime/minio) build independently. The summary gate `CI quality gate` fails when any prerequisite fails or is skipped. Actions are pinned to verified commit SHAs and the workflow defaults to `contents:read` only. `.github/branch-protection.json` describes the master protection rule and should be applied after a successful remote run exists.

[← Back to README](../../README_EN.md)
