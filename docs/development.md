# 本地开发

本文档覆盖 SKYCloud 的本地开发启动方式、测试套件与运行方式，以及依赖管理与 CI 流程。

Python 依赖由仓库根目录的 `pyproject.toml` 声明、同目录的 `uv.lock` 锁定，需要 uv 0.13.0 与 Python 3.11。默认安装 `dev` 组（包含 `test` 与 `quality`）；`uv run --locked` 复用该环境，`uv.lock` 与 `pyproject.toml` 不一致时直接失败。以下命令未注明时均在仓库根目录执行。

## 启动服务

```bash
uv sync --locked   # 创建 .venv 并安装 dev 依赖

# 后端（需 PostgreSQL / Redis / RabbitMQ）；进程入口以 backend 为工作目录
cd backend
uv run --locked python -m entry.api          # API
uv run --locked python -m entry.worker       # Worker
uv run --locked python -m entry.mcp_run      # MCP Server

# 前端（Vite dev proxy → localhost:5000）
cd frontend && npm ci && npm run dev
```

## 测试

测试配置位于根 `pyproject.toml` 的 `[tool.pytest.ini_options]`：`testpaths = ["backend/tests"]`、`pythonpath = ["backend"]`，因此可在仓库根目录或 `backend` 中运行；`tests/real` 与各集成用例默认跳过。

```bash
uv run --locked pytest --ignore=backend/tests/real
```

测试上传目录位于系统临时目录，并在测试进程退出时自动清理。

需要真实基础设施的集成用例通过 `RUN_*` 环境变量显式开启，例如 `RUN_MIGRATION_TESTS`、`RUN_VECTOR_TESTS`、`RUN_UPLOAD_STORAGE_TESTS`、`RUN_TASK_QUEUE_TESTS`、`RUN_RUNTIME_SECURITY_TESTS`；这些用例会创建并清理独立容器，完整清单见 `backend/tests/integration/`。前端在 `frontend` 目录运行 `npm run test:unit`、`npm run lint:check`、`npm run type-check` 和 `npm run build`。

## Python 严格静态检查

在仓库根目录运行。Ruff、Pyright、Mypy 的配置全部位于根 `pyproject.toml`（`[tool.ruff]`、`[tool.pyright]`、`[tool.mypy]`），没有独立配置文件，命令直接指向它：

```bash
uv run --locked ruff check backend/app backend/entry backend/runtime_controller backend/runtime_common --output-format json --exit-zero --output-file /tmp/skycloud-ruff-all.json
uv run --locked python scripts/check_ruff_baseline.py --report /tmp/skycloud-ruff-all.json --baseline backend/ruff-baseline.json
uv run --locked pyright --project pyproject.toml
uv run --locked mypy --config-file pyproject.toml
```

Pyright 使用 `typeCheckingMode: strict`；Ruff 没有 `strict` 模式，使用 `lint.select = ["ALL"]` 启用全部稳定规则，不设置忽略项。互斥的文档字符串规则由 Ruff 自动择一并给出警告。Ruff 的完整报告包括经逐项审查并精确记录的例外；比较器会展示所有诊断，遇到新诊断或过期条目即失败。`--exit-zero` 只用于生成完整报告，不能跳过后续比较。两项检查覆盖生产代码，与 CI 一致；现有 mypy 检查保留。

`cacheable` 是唯一的缓存写入装饰器：先读 Redis，未命中再执行函数并写入。缓存失效依靠键回调而非删除无后缀键——`key`、`generation_key` 把条目绑定到 SQL revision 或旋转后的命名空间，`cache_allowed` 拒绝可能回滚的写事务。三者作为 `cacheable` 的关键字参数传入，必须与被装饰函数的参数签名兼容。`CacheableParams` 保存前缀、过期时间和 `None` 策略。

## 依赖管理与 CI

Python 依赖只有一份锁文件：根目录 `uv.lock`。`[project].dependencies` 是运行时依赖，`test`、`quality`、`controller` 是依赖组，默认 `dev` 组包含 `test` 与 `quality`。Controller 服务使用独立依赖组安装，不引入根运行时依赖：

```bash
uv sync --locked --no-default-groups --only-group controller
```

生产镜像在构建时以 `uv sync --locked --no-dev` 安装运行时依赖，Controller 镜像以 `uv sync --locked --no-default-groups --only-group controller` 安装；CI 执行 `uv sync --locked`（集成任务使用 `uv sync --locked --no-default-groups --group test`）后运行检查。更新依赖时执行 `uv lock --upgrade`，审查 `uv.lock`，再 `uv sync --locked`、运行测试并执行 `uv run --locked python scripts/check-python-runtime.py`。uv 版本由 `[tool.uv] required-version` 固定为 0.13.0，Python 版本由 `requires-python = ">=3.11,<3.12"` 固定为 3.11。生产镜像（Python、Node、nginx、PostgreSQL、Redis、RabbitMQ）固定为支持 amd64/arm64 的镜像索引摘要，后端 apt 包使用签名 Debian 快照。

前端依赖仍由 `frontend/package-lock.json` 锁定，与上述 uv 项目相互独立；CI 使用 `npm ci` 安装并以 `npm audit --audit-level=low` 审计全部依赖。出现安全公告时，在 `frontend` 目录运行 `npm audit fix` 更新兼容范围内的锁定版本，提交锁文件，并重新运行 `npm ci`、审计及上述前端检查；不要降低审计门槛或跳过开发依赖。

CI 定义在 `.github/workflows/ci.yml`：后端安装 uv 0.13.0、以 `uv sync --locked` 安装依赖并运行测试，前端执行依赖审计、lint、类型检查、行为测试和构建；集成任务运行 PostgreSQL 迁移、RabbitMQ 交付/outbox/崩溃恢复检查，以及真实 OpenCode 沙箱启动、配置重载和历史 generation 回收；五个镜像（backend/controller/frontend/runtime/minio）独立构建。汇总检查固定为 `CI quality gate`，任一前置任务失败或跳过都不能通过。Actions 固定到已核实的提交 SHA，工作流默认仅 `contents:read`。`.github/branch-protection.json` 是供 master 使用的保护规则，需在远端取得成功运行后再应用。

[← 返回 README](../README.md)
