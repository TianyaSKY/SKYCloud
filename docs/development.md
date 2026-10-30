# 本地开发

本文档覆盖 SKYCloud 的本地开发启动方式、测试套件与运行方式，以及依赖锁定与 CI 流程。

## 启动服务

```bash
# 后端（Python 3.11；需 PostgreSQL / Redis / RabbitMQ）
cd backend && python -m pip install --require-hashes --only-binary=:all: -r requirements.lock
python -m entry.api          # API
python -m entry.worker       # Worker
python -m entry.mcp_run      # MCP Server

# 前端（Vite dev proxy → localhost:5000）
cd frontend && npm ci && npm run dev
```

## 测试

在 `backend` 目录安装带哈希的测试依赖，再运行常规测试套件（`tests/real` 与各集成用例默认跳过）：

```bash
python -m pip install --require-hashes --only-binary=:all: -r requirements-test.lock
python -m pytest --ignore=tests/real
```

测试上传目录位于系统临时目录，并在测试进程退出时自动清理。

需要真实基础设施的集成用例通过 `RUN_*` 环境变量显式开启，例如 `RUN_MIGRATION_TESTS`、`RUN_VECTOR_TESTS`、`RUN_UPLOAD_STORAGE_TESTS`、`RUN_TASK_QUEUE_TESTS`、`RUN_RUNTIME_SECURITY_TESTS`；这些用例会创建并清理独立容器，完整清单见 `backend/tests/integration/`。前端在 `frontend` 目录运行 `npm run test:unit`、`npm run lint:check`、`npm run type-check` 和 `npm run build`。

## Python 严格静态检查

在仓库根目录运行，使用 Python 3.11 环境：

```bash
python -m pip install --require-hashes --only-binary=:all: -r backend/requirements-test.lock -r backend/requirements-quality.lock
python -m ruff check backend/app backend/entry backend/runtime_controller --config backend/ruff.toml --output-format json --exit-zero --output-file /tmp/skycloud-ruff-all.json
python scripts/check-ruff-baseline.py --report /tmp/skycloud-ruff-all.json --baseline backend/ruff-baseline.json
python -m pyright --project pyrightconfig.json
python -m mypy --config-file backend/mypy.ini
```

Pyright 使用 `typeCheckingMode: strict`；Ruff 没有 `strict` 模式，使用 `lint.select = ["ALL"]` 启用全部稳定规则，不设置忽略项。互斥的文档字符串规则由 Ruff 自动择一并给出警告。Ruff 的完整报告包括经逐项审查并精确记录的例外；比较器会展示所有诊断，遇到新诊断或过期条目即失败。`--exit-zero` 只用于生成完整报告，不能跳过后续比较。两项检查覆盖生产代码，与 CI 一致；现有 mypy 检查保留。

缓存装饰器的 `CacheableParams` 保存前缀、过期时间和 `None` 策略；`key`、`generation_key` 和 `cache_allowed` 作为 `cacheable` 的关键字参数传入，必须与被装饰函数的参数签名兼容。`cache_evict` 和 `cache_put` 的 `key` 回调遵循同一约束。

## 依赖与 CI

Python 的运行、测试和 Controller 依赖分别锁定在 `backend/requirements.lock`、`backend/requirements-test.lock` 和 `backend/runtime_controller/requirements.lock`，包含全部传递依赖版本及发布文件的 SHA-256；输入文件仍是对应的 `requirements*.txt`，安装时使用 `.lock`。更新依赖时用 uv 0.12.1 运行 `python scripts/lock-python.py --upgrade`，随后审计锁文件、运行测试并执行 `python scripts/check-python-runtime.py`。生产镜像（Python、Node、nginx、PostgreSQL、Redis、RabbitMQ）固定为支持 amd64/arm64 的镜像索引摘要，后端 apt 包使用签名 Debian 快照。

前端依赖锁定在 `frontend/package-lock.json`，CI 使用 `npm ci` 安装并以 `npm audit --audit-level=low` 审计全部依赖。出现安全公告时，在 `frontend` 目录运行 `npm audit fix` 更新兼容范围内的锁定版本，提交锁文件，并重新运行 `npm ci`、审计及上述前端检查；不要降低审计门槛或跳过开发依赖。

CI 定义在 `.github/workflows/ci.yml`：后端安装哈希锁并运行测试，前端执行依赖审计、lint、类型检查、行为测试和构建；集成任务运行 PostgreSQL 迁移、RabbitMQ 交付/outbox/崩溃恢复检查，以及真实 OpenCode 沙箱启动、配置重载和历史 generation 回收；五个镜像（backend/controller/frontend/runtime/minio）独立构建。汇总检查固定为 `CI quality gate`，任一前置任务失败或跳过都不能通过。Actions 固定到已核实的提交 SHA，工作流默认仅 `contents:read`。`.github/branch-protection.json` 是供 master 使用的保护规则，需在远端取得成功运行后再应用。

[← 返回 README](../README.md)
