# 部署与运维

本文档覆盖 SKYCloud 的部署流程（含运行时与 MinIO 镜像构建）、管理员与密钥初始化、数据库升级、密码与会话变更，以及含 SKYcode 工作区的服务停止方式。

## 前置条件与镜像构建

> 环境要求：uv 0.13.0（Python 3.11）、Docker + Docker Compose；运行时支持 arm64 和 amd64。

Python 依赖只在根目录维护一份：`pyproject.toml` 声明、`uv.lock` 锁定，宿主机脚本只依赖标准库，通过 `uv run --no-project python` 运行。后端与 Controller 镜像构建时分别执行 `uv sync --locked --no-dev` 和 `uv sync --locked --no-default-groups --only-group controller`，不再使用独立的 requirements 锁文件。

完整的首次部署命令：

```bash
git clone https://github.com/TianyaSKY/SKYCloud && cd SKYCloud
cp .env.example .env
# 编辑 .env，仅需填写 Chat 和 Embedding 的 6 项模型配置（CHAT_* / EMB_*）
uv run --no-project python scripts/start.py
docker compose exec backend-api python -m entry.bootstrap_admin --username admin
```

统一启动脚本 `scripts/start.py` 会自动完成：
1. 系统密钥初始化（写入 `.skycloud/generated.env`，文件权限为私有 `0600`，绝不覆盖已有密钥）；
2. 校验并构建 Runtime 沙箱与 MinIO 镜像，不可变镜像 ID 存放在 `.skycloud/generated.env`；
3. 执行 `docker compose up -d --build`（可传入 `--dev` 启用开发端口映射，或 `--check-only` 仅校验环境）。

换 Docker 主机或需要更新底层环境时，可运行 `uv run --no-project python scripts/start.py --rebuild-images`。

若从旧版（86 项环境变量）升级，运行以下命令即可将既有数据库密码、Token 密钥和自定义部署项安全迁移到新结构：
```bash
uv run --no-project python scripts/migrate-env.py
```
迁移脚本会将旧 `.env` 备份为 `.env.bak`，保留当前有效密钥至 `.skycloud/generated.env`，仅提取 6 个模型变量放入新 `.env`，并将非默认部署配置输出至 `.env.advanced`。原有数据卷和数据库密码完全不受影响。

运行时构建脚本校验锁定的官方 CLI 压缩包 SHA-256，使用固定 glibc 基础镜像和 Debian 软件包快照，并将本机不可变镜像 ID 写入 `.skycloud/generated.env`，保留其他配置与密钥。官方 Alpine 镜像的 PTY 原生库存在 glibc 兼容问题，因此部署必须先执行此构建步骤；换 Docker 主机时重新构建。沙箱普通 `/tmp` 保持 `noexec`，仅限额 64 MiB 的私有 `/tmp/.native` 允许加载 PTY 原生库。

MinIO 的原公共镜像地址已不可拉取，同样由构建脚本从 `backend/minio_image/lock.json` 锁定的官方源码构建：校验源码 SHA-256，固定 Go 工具链，并使用上游 `go.sum` 校验全部 Go 依赖，最后把本机不可变镜像 ID 写入 `.skycloud/generated.env`；换 Docker 主机时重新构建。

## 首次启动与管理员

启动后访问 `http://app.skycloud.localhost`（默认端口 80），使用显式创建的管理员账号登录。创建命令在终端交互读取密码，不接受命令行密码。系统不会自动创建管理员；JWT 密钥缺失、过短或仍为占位值时拒绝启动。

历史版本自带的默认管理员账号会阻止应用启动，也无法登录。先备份数据库、完成迁移，再用部署终端交互重置密码：

```bash
docker compose run --rm --no-deps backend-api python -m entry.reset_admin --username admin
docker compose up -d
```

该命令只重置指定的现有管理员，不创建或提升普通账户；保留用户数据，并撤销其旧会话和令牌。密码不会放入命令行参数。

## 配置与密钥

SKYCloud 采用分层配置架构：
1. **用户模型配置（`.env`）**：仅需填写 6 个与大模型供应商相关的参数（Chat 与 Embedding 的 API_URL、API_KEY 与 MODEL），避免因繁杂配置项导致出错。
2. **系统私有凭证（`.skycloud/generated.env`）**：在首次启动时由脚本自动生成（PostgreSQL、RabbitMQ、MinIO 密码，以及 JWT、OpenCode、Runtime Controller 密钥和镜像 ID）。以严格的私有权限 `0600` 写入，绝不提交至 Git，重启与再次构建均不会覆盖已有凭证。
3. **可选高级配置（`.env.advanced`）**：如需自定义服务对外端口（如将前端暴露在 8080）、域名、HTTPS，或调整数据库连接池、上传配额等，可根据 `.env.advanced.example` 创建 `.env.advanced`。

应用启动会检查三个主密钥互不相同、基础设施密码至少 12 字符且不为默认值。已有 PostgreSQL/RabbitMQ 数据卷中的密码不会随配置自动改变：先通过基础设施的受信管理工具轮换实际密码，再同步 `.skycloud/generated.env` 中的凭据。

## 数据库初始化与重建

唯一 schema revision 为 `0001_rebuild`。API、Worker、MCP 和模型网关启动时只读取 Alembic 版本，不建表；版本缺失或不匹配时拒绝启动。该静态基线只初始化空 PostgreSQL 数据库。

新部署或已明确授权的全量重建，先配置隔离的空数据库，再执行：

```bash
docker compose build
docker compose run --rm migrate
docker compose up -d
```

宿主机开发先设置 `DATABASE_URL`，在 `backend` 中执行 `uv run --locked python -m entry.migrate`。既有 revision 或任何未版本化现存表都会被拒绝；不升级旧 schema、不修补旧列、不 stamp，也不转换历史业务数据。切换既有部署前必须停止所有旧写入方，验证备份恢复流程，并只在明确授权的项目范围重建数据库及关联文件/对象；部署配置与外部凭据另行保留。schema 基线禁止 downgrade，回退依赖经验证的整套备份。

真实迁移测试会创建并清理独立 PostgreSQL 容器：

```bash
RUN_MIGRATION_TESTS=1 uv run --locked pytest backend/tests/integration/test_migrations.py
```

## 密码与会话变更

修改密码（含管理员代改）会在同一事务中递增目标账户的认证版本，撤销其旧 Web 会话、外部 MCP Token 和 Runtime 凭据。本人改密后前端退出登录；其他浏览器会在下一次请求收到 401。外部 MCP 客户端需生成并重新配置 Token，Runtime 需重新启动以获取新凭据。启用认证版本检查后，历史 Web Token 一律失效，需要重新登录。

## 停止服务（含 SKYcode 工作区）

SKYcode 工作区是后端动态 `docker run` 创建的，**不在** `docker-compose.yml` 中，单独执行 `docker compose down` 不会停掉它们。

请使用仓库脚本一并清理：

```powershell
# Windows PowerShell
.\scripts\compose-down.ps1
# 如需同时删除数据卷
.\scripts\compose-down.ps1 -v
```

```bash
# Linux / macOS / Git Bash
chmod +x scripts/compose-down.sh   # 仅首次
./scripts/compose-down.sh
./scripts/compose-down.sh -v
```

临时手动清理也可：

```bash
docker rm -f $(docker ps -aq --filter label=skycloud.component=workspace) 2>/dev/null
docker rm -f $(docker ps -aq --filter name=skycloud-workspace-) 2>/dev/null
docker compose down
```

[← 返回 README](../README.md)
