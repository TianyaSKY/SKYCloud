<div align="center">
  <h1>SKYCLOUD</h1>
  <p><strong>AI 驱动的云文件管理平台</strong></p>
  <p>
    <img src="https://img.shields.io/badge/Python-3.10%2B-blue" alt="Python">
    <img src="https://img.shields.io/badge/Vue.js-3.x-4FC08D" alt="Vue">
    <img src="https://img.shields.io/badge/FastAPI-0.115-009688" alt="FastAPI">
    <img src="https://img.shields.io/badge/PostgreSQL-15-336791" alt="PostgreSQL">
    <img src="https://img.shields.io/badge/Docker-Enabled-2496ED" alt="Docker">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </p>
  <p><a href="./README_EN.md">English</a></p>
</div>

---

SKYCloud 是一个 AI 增强的云文件管理系统，提供从文件存储、智能检索到自主执行的一站式体验。

## 功能亮点

- **文件管理** — 上传（分片 / 秒传）、下载、预览、批量操作、文件夹树、格式转换
- **AI 对话（RAG）** — 多维关键词改写 → Multi-Query 向量召回 → RRF 融合 → 可选 Rerank → SSE 流式输出，支持图片引用
- **双模式 AI 助手** — 快速模式与专家模式使用独立会话；专家模式通过受控 OpenCode Runtime 支持工具调用、权限确认、取消与 Diff
- **AI 文件整理** — LangGraph ReAct Agent 自动分类，增量 / 全量模式，整理后收件箱通知
- **全能工作区** — 独立 Docker 沙箱 + MCP 协议，AI Agent 可读写云盘、运行代码、自动化任务（Manus 风格）
- **MCP 服务** — 16 个工具 / 4 个 Prompt / 2 个 Resource，Claude Desktop、Cursor 等客户端直接接入；支持不依赖 curl 的 `upload_file` 直传
- **分享 & 收件箱** — 带过期时间的分享链接、系统通知推送
- **Token 用量追踪** — 对话 / 索引 / 整理的消耗统计，含管理员汇总视图
- **性能优化** — Bloom Filter 权限前置、Redis 缓存、RabbitMQ 异步索引、并行 Embedding

| 智能助手对话                           | 文件预览                             |
|----------------------------------|----------------------------------|
| ![智能助手对话](images/agent_chat.png) | ![文件预览](images/file_preview.png) |

## 架构

![系统架构](images/architecture.png)

| 层级      | 技术                                                   |
|---------|------------------------------------------------------|
| 后端      | FastAPI · SQLAlchemy · Pydantic                      |
| AI      | LangChain · LangGraph · OpenAI API（兼容 SiliconFlow 等） |
| 向量库     | PostgreSQL 15 + pgvector（1024 维）                     |
| 队列 / 缓存 | RabbitMQ 3.13 · Redis 7                              |
| 前端      | Vue 3 · TypeScript · Vite · Arco Design              |
| MCP     | FastMCP（Streamable HTTP）                             |
| 部署      | Docker Compose                                       |

## 快速开始

> 环境要求：Python 3.11+、Docker + Docker Compose；运行时支持 arm64 和 amd64。

```bash
git clone https://github.com/TianyaSKY/SKYCloud && cd SKYCloud
python3 scripts/generate-env.py
python3 scripts/build-runtime-image.py --env-file .env
python3 scripts/build-minio-image.py --env-file .env
# 编辑 .env，至少填写 Chat 和 Embedding 模型的 API_URL / API_KEY / MODEL
docker compose up -d --build
docker compose exec backend-api python -m entry.bootstrap_admin --username admin
```

运行时构建脚本校验锁定的官方 CLI 压缩包 SHA-256，使用固定 glibc 基础镜像和 Debian 软件包快照，并将本机不可变镜像 ID 写入 `.env`，保留其他配置与密钥。官方 Alpine 镜像的 PTY 原生库存在 glibc 兼容问题，因此部署必须先执行此构建步骤；换 Docker 主机时重新构建。沙箱普通 `/tmp` 保持 `noexec`，仅限额 64 MiB 的私有 `/tmp/.native` 允许加载 PTY 原生库。

工作区 UI 使用独立主机上的 HttpOnly 会话，单次租期十分钟。主页面打开期间每五分钟申请一次性票据，通过隐藏 iframe 更新 Cookie，保留编辑界面；返回可见页面时也会续期。续期失败会关闭工作区入口。终端和事件流仍受各自租期限制，重连会使用当前 Cookie；权限撤销不依赖页面主动关闭。

浏览器访问令牌只保存在内存中，有效期十五分钟；刷新凭证使用 host-only HttpOnly Strict Cookie，HTTPS 下带 Secure 和 `__Host-` 前缀，绝对有效期七天且每次使用都会轮换。Redis 只保存哈希索引和会话元数据。重放旧刷新凭证、退出登录或修改密码会撤销访问权限；浏览器通过同源 Web Lock 串行化各标签页的 Cookie 轮换。Redis 故障时续期拒绝，不会静默重建会话。非 localhost 部署必须配置 HTTPS `APP_ORIGIN`。升级会清除历史 localStorage 登录信息，需要重新登录。

前端 CSP 禁止内联脚本和 eval；样式保留组件库所需的 inline 支持。部署自定义运行时域名时，将 `RUNTIME_UI_CSP_SOURCE` 配置为对应的 CSP frame-source，例如 `https://*.runtime.example.com`，与 `RUNTIME_UI_ORIGIN_TEMPLATE` 一致。`/auth/refresh` 和 `/auth/logout` 必须使用同源 POST、精确的 Origin 及 `X-Skycloud-Session: 1` 请求头。

启动后访问 `http://app.skycloud.localhost`（默认端口 80），使用显式创建的管理员账号登录。创建命令在终端交互读取密码，不接受命令行密码。系统不会自动创建管理员；JWT 密钥缺失、过短或仍为占位值时拒绝启动。

配置生成脚本独立生成各项密钥，以权限 0600 写入，并拒绝覆盖已有文件。应用启动会检查三个主密钥互不相同、基础设施密码至少 12 字符且不为默认值。已有 PostgreSQL/RabbitMQ 数据卷中的密码不会随 `.env` 自动改变：先通过基础设施的受信管理工具轮换实际密码，再同步配置。

历史版本自带的默认管理员账号会阻止应用启动，也无法登录。先备份数据库、完成迁移，再用部署终端交互重置密码：

```bash
docker compose run --rm --no-deps backend-api python -m entry.reset_admin --username admin
docker compose up -d
```

该命令只重置指定的现有管理员，不创建或提升普通账户；保留用户数据，并撤销其旧会话和令牌。密码不会放入命令行参数。

### 数据库升级

API、Worker、MCP 和模型网关启动时只读取 Alembic 版本，不建表、不补列。Compose 的 `migrate` 服务成功后才启动它们；版本缺失或不匹配时拒绝启动。

已有部署先备份数据库，再执行：

```bash
docker compose build
docker compose run --rm migrate
docker compose up -d
```

宿主机开发先设置 `DATABASE_URL`，在 `backend` 中执行 `python -m entry.migrate`。迁移兼容未版本化数据库，保留账号和文件数据，补齐历史缺失列并修复向量索引。迁移失败会回滚并阻止应用启动；不要手动 stamp 版本。初始版本禁止删除数据的降级，回退部署前应验证数据库备份恢复流程。

真实迁移测试会创建并清理独立 PostgreSQL 容器：

```bash
cd backend
RUN_MIGRATION_TESTS=1 python -m pytest tests/integration/test_migrations.py
```

修改密码（含管理员代改）会在同一事务中递增目标账户的认证版本，撤销其旧 Web 会话、外部 MCP Token 和 Runtime 凭据。本人改密后前端退出登录；其他浏览器会在下一次请求收到 401。外部 MCP 客户端需生成并重新配置 Token，Runtime 需重新启动以获取新凭据。升级认证版本迁移后，历史 Web Token 一律失效，需要重新登录。

### 停止服务（含 SKYcode 工作区）

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

## RAG 检索增强

![RAG Pipeline](images/rag_pipeline.png)

**离线**：文件上传 → RabbitMQ → Worker 生成描述 + 1024 维 Embedding → pgvector

**在线**：6 维关键词改写 → 多查询生成 → 并行向量召回 → RRF 融合 → Rerank → SSE 流式输出

## 双模式 AI 助手

助手入口提供两套互不污染的会话引擎：

- **快速模式**：只读当前工作空间资料，复用 RAG 检索链，不启动 Runtime，适合查找、总结和文档问答。
- **专家模式**：仅向 `editor` / `admin` 开放，按用户和工作空间启动独立 OpenCode Runtime，支持多步骤工具调用、命令权限确认、取消、文件 Diff 和会话恢复。

后端统一入口为 `/api/assistant`，会话、消息和 Run 持久化在 `assistant_conversations`、`assistant_messages`、`assistant_runs` 三张表中。专家 Runtime 使用短期、可撤销的 MCP Token 和 OpenCode Basic Auth；普通 REST 依赖拒绝 Runtime Token，Runtime 凭证只可访问专用 MCP 服务。

默认通过环境变量控制渐进式发布：`ASSISTANT_V2_ENABLED`、`ASSISTANT_EXPERT_ENABLED`、`ASSISTANT_PERMISSION_UI_ENABLED`、`ASSISTANT_RUNTIME_LOCK_TTL`。生产部署应设置独立的 `OPENCODE_SERVER_SECRET`，并使用固定版本或 digest 的 `OPENCODE_IMAGE`。

切换工作空间或卸载助手会关闭旧流，旧会话请求的响应不会修改当前会话。连接中断时会重新查询服务器上的活动任务，状态查询失败时暂停发送；初始化失败后可关闭并重新打开助手重试。取消接口确认收到请求后，前端继续等待任务实际结束；取消失败保留原任务。权限确认仅对当前请求生效，不保存永久允许规则，取消已确认的任务不再接受新的权限答复。

助手流使用内存 Bearer 凭据；仅在请求被 401 拒绝时续期并重试一次，不重放已经接收的流。解析器支持跨块 UTF-8、多行事件和 CRLF，限制未结束事件大小，并在退出或处理失败时释放读取器。

### Runtime 安全与开发端口

专家 Runtime 使用构建脚本生成的本地不可变 OpenCode 镜像 ID；本地镜像缺失时拒绝启动，需先重新执行构建脚本。容器以 UID 10001 运行，移除所有额外能力，禁止提权，并设置 CPU、内存和 PID 限制。根文件系统只读，临时目录分别有容量上限：工作目录 512 MiB、用户目录 256 MiB、`/tmp` 64 MiB；容器停止后这些临时数据会清空，需保留的文件应上传到云盘。

Runtime 位于 `runtime-network` 专用内网，不能访问 PostgreSQL、Redis、RabbitMQ 或 MinIO，也没有直接外网出口。模型请求通过 `llm-gateway`：只有网关持有 `CHAT_API_KEY`，Runtime 只获得一小时以内有效、可撤销、绑定用户/工作空间/Runtime/模型的调用凭据。额度按用户、工作空间和 Runtime 原子计数，刷新凭据或重建容器不会重置用户额度；请求失败及断开流仍计入预留额度。

可通过 `RUNTIME_LLM_HOURLY_TOKEN_BUDGET`、`RUNTIME_LLM_HOURLY_REQUEST_LIMIT` 和 `RUNTIME_LLM_MAX_OUTPUT_TOKENS` 调整额度。计费采用保守的输入字节数加输出 Token 上限，网关支持文本和工具调用。公开模型密钥不会写入 Runtime 配置。

生产 Compose 只发布前端端口，MCP 通过同源 `/mcp` 访问。需要从宿主机访问开发服务时，显式使用 loopback 覆盖：

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

API 不挂载 Docker Socket，也不加入 Runtime 网络。独立 `runtime-controller` 仅接受固定镜像、沙箱策略和托管容器的生命周期/配置操作；不提供任意 Docker 参数或 exec 接口。它以非 root 身份运行，仅在独立控制网卡监听，通过独立随机 `RUNTIME_CONTROLLER_TOKEN` 鉴权，且只接收自身所需配置，不加载数据库或供应商密钥。

本地混合开发时，API/Worker 可在宿主机运行，并通过开发覆盖发布的 `http://127.0.0.1:5003` 访问 Controller。MCP、模型网关和 Controller 继续在 Compose 中运行，以保持 Runtime 内网边界。Runtime 不发布宿主机端口，HTTP、会话和 SSE 均经 Controller 转发。Linux 部署需设置 `DOCKER_SOCKET_GID` 为宿主机 Socket 的实际组 ID；Controller 凭据和 `OPENCODE_SERVER_SECRET` 必须分别生成，至少 32 字符，禁止使用示例占位值。

无需供应商 Key 的真实安全测试会创建并清理临时 Docker 网络和容器，验证 Redis 并发额度、网络边界以及 OpenCode 的非 root 启动、鉴权和配置重载：

```bash
cd backend
SKYCLOUD_TEST_RUNTIME_IMAGE=$(docker image inspect skycloud-opencode:1.18.11 --format '{{.Id}}') RUN_RUNTIME_SECURITY_TESTS=1 python -m pytest tests/integration/test_runtime_security.py
```

官方 OpenCode 冒烟测试和可选模型测试位于 `backend/tests/integration/test_expert_opencode_e2e.py`。模型测试只接受网关地址 `OPENCODE_E2E_GATEWAY_URL`、短期凭据 `OPENCODE_E2E_RUNTIME_LLM_TOKEN` 和允许的模型 `OPENCODE_E2E_MODEL`，禁止传入供应商 Key。

## MCP 接入

MCP Server 独立容器运行，生产部署通过前端同源 **`/mcp`** 访问；开发覆盖可开放本机端口 **5001**。登录后点击右上角头像 → **MCP Token**，显式生成外部客户端凭据。完整 Token 仅在生成时显示，关闭窗口后无法恢复，只能重新生成；后端只保存 SHA-256 哈希和指纹，读取接口只返回元数据。升级哈希存储迁移后历史外部 Token 会撤销，请重新生成并配置客户端。
工作区在创建/启动/重启时会自动注入独立的短期 Runtime Token，无需手动「连接 MCP」。

| 工作区 / MCP              | Token 用量                            |
|------------------------|-------------------------------------|
| ![MCP](images/mcp.png) | ![Token 用量](images/token-usage.png) |

## 全能工作区

每个工作区运行在独立 Docker 容器中，通过 MCP 协议让 AI 在沙箱内自主执行任务并与云盘交互。

| 分享管理                    | 工作区                          |
|-------------------------|------------------------------|
| ![分享](images/share.png) | ![工作区](images/workspace.png) |

## 本地开发

```bash
# 后端（Python 3.11；需 PostgreSQL / Redis / RabbitMQ）
cd backend && python -m pip install --require-hashes --only-binary=:all: -r requirements.lock
python -m entry.api          # API
python -m entry.worker       # Worker
python -m entry.mcp_run      # MCP Server

# 前端（Vite dev proxy → localhost:5000）
cd frontend && npm ci && npm run dev
```

## 项目结构

```
SKYCloud/
├── backend/
│   ├── app/
│   │   ├── api/            # HTTP：routers / schemas / dependencies / factory
│   │   ├── mcp/            # MCP 协议适配（tools / resources）
│   │   ├── workers/        # 异步任务：索引 / 整理 / 格式转换
│   │   ├── services/       # 共享业务逻辑（API / MCP / Worker 共用）
│   │   ├── models/         # SQLAlchemy 模型
│   │   ├── infra/          # 缓存 / 队列 / 上传 / 时间工具
│   │   ├── extensions.py   # DB / Redis 等基础设施
│   │   └── exceptions.py   # 统一异常
│   ├── run.py              # API 入口
│   ├── tasks.py            # Worker 入口
│   └── mcp_run.py          # MCP 入口
├── frontend/src/
│   ├── api/                # Axios 封装
│   ├── components/         # 通用组件
│   ├── views/              # 页面
│   ├── hooks/              # Composition API
│   └── router/             # 路由
├── docker-compose.yml
└── .env.example
```

## License

MIT

上传大小限制：默认单文件 256 MiB、头像 5 MiB、单个请求 512 MiB、分片 64 MiB。后端在接收请求体和写入文件时均检查实际字节数（包括没有 Content-Length 的请求），超限返回 413 并清理临时文件。`.env.example` 中的 `UPLOAD_*` 可调整且必须为正整数；提高请求上限时还需同步调整 `frontend/nginx.conf` 的 `client_max_body_size`。

助手执行进程在开始和流心跳期间续写数据库运行心跳；其他进程恢复中断任务时锁定同一条记录并读取最新心跳，避免将长任务误判为服务重启。默认失联宽限期 45 秒，`ASSISTANT_ABANDONED_RUN_GRACE_SECONDS` 最小有效值为 30 秒；历史没有心跳的运行仍按开始时间恢复。恢复出的运行在没有本地 SSE 时每 3 秒串行同步状态与历史，临时失败退避至最多 30 秒；确认终态后停止，切换或卸载会清理轮询。取消或恢复终止执行时会关闭事件生成器，等待其生产任务退出。

Runtime 模型配置的输出上限与网关的 `RUNTIME_LLM_MAX_OUTPUT_TOKENS` 保持一致（默认 4096），避免 OpenCode 默认请求 32000 而被网关拒绝。模型上下文预算由 `RUNTIME_LLM_CONTEXT_TOKENS` 控制（默认 65536），必须大于输出上限；配置使用固定版本 OpenCode 的 [model.limit 契约](https://github.com/anomalyco/opencode/blob/v1.18.11/packages/core/src/v1/config/provider.ts)。

完整隔离模型检查可在 backend 目录执行 `RUN_EXPERT_GATEWAY_TESTS=1 SKYCLOUD_TEST_RUNTIME_IMAGE=sha256:<Runtime镜像ID> SKYCLOUD_TEST_CONTROLLER_IMAGE=<本地Controller镜像> python -m pytest tests/integration/test_expert_gateway.py`，需要 Docker 和本地 `skycloud-frontend:locked-test` 镜像作为测试代理。测试使用独立 PostgreSQL、Redis、Controller、非 root Runtime 和真实 MCP 子进程，唯一模拟边界是本地模型端点；验证普通流式回答、一次允许执行合成命令、拒绝后命令未执行，以及数据库授权说明恢复、MCP 连接、作用域凭据及 Redis 配额，并清理自身资源。

授权事件适配固定版本 OpenCode 的 [PermissionRequest 契约](https://github.com/anomalyco/opencode/blob/v1.18.11/packages/sdk/js/src/v2/gen/types.gen.ts)：从权限类别、操作元数据和匹配范围生成说明，不把消息/工具调用指针显示为操作内容。超过展示上限时明确标记截断，缺少当前会话归属的事件被丢弃；响应客户端只发送一次允许或拒绝，不发送永久授权或记忆字段。

专家待授权运行将授权 ID、标题与工具说明持久化在既有运行元数据中；标题最多 2000 字符、工具说明最多 4000 字符，不保存原始提供方事件。活动运行接口仅在仍等待该授权、尚未响应且未请求取消时返回 `pending_permission`。前端恢复时核对会话、助手消息和授权 ID，信息缺失或不匹配时保留取消入口，不提供盲目授权。心跳和普通状态消息不会解除授权等待；授权仍仅允许一次性同意或拒绝。

工作空间列表刷新采用最后一次请求的结果，保留用户在加载期间的最新选择；成员资格消失后切换到可访问空间，列表为空时清除当前空间和写权限。退出或重新登录会清除空间状态并丢弃旧请求。成员管理仅管理员可修改成员，所有者不可被移除或降级，空间删除仅向所有者显示。确认操作固定原空间及成员，抽屉切换、关闭、登录变化和页面卸载后旧操作失效；成功修改自己的角色或移除自己时立即撤销本地权限，再刷新服务端成员状态。

前端按 2 MiB 读取文件并增量计算 SHA-256，上传任务固定创建时的账号、工作空间和目录；切换账号或空间、取消任务及页面卸载会中止请求和等待队列。全局最多同时执行 3 个文件上传，仅临时网络或服务端错误会重试分片，完成元数据提交后进度才显示 100%。分片恢复标识包含文件内容与所属上下文；浏览器本地保存的仅是恢复标识，过期会话返回 409 时重新初始化。文件选择器打开后若目录、空间、登录会话或写权限变化，原选择被丢弃，需在当前目录重新选择。

可选端到端检查：在 backend 目录运行 `RUN_FULL_BROWSER=1 SKYCLOUD_TEST_RUNTIME_IMAGE=sha256:<Runtime镜像ID> SKYCLOUD_TEST_MINIO_IMAGE=sha256:<MinIO镜像ID> python -m pytest tests/integration/test_full_browser.py`。读取 `/private/tmp/skycloud-full-browser.json` 中的临时浏览器地址；保持测试运行，在 frontend 目录执行 `node scripts/check-upload-http.mjs`，用真实前端上传代码验证小文件、分片、下载 SHA-256 和秒传。完成浏览器登录、刷新和 Runtime 页面检查后，向元数据指定的 completion_file 写入 `verified`，测试将清理自身容器。HTTP 脚本仅替代路由和通知显示，不能证明原生文件选择器操作成功；`--build-only` 只检查脚本打包。

上传配额默认每用户 10 GiB、每空间 50 GiB，统计逻辑文件（包括秒传副本）与未到期的预留容量；每日按 UTC 日期限制 200 次、5 GiB，在上传预留成功时扣除，中止和失败不返还每日额度。用户最多 8 个、空间最多 32 个活动上传，预留 1 小时后过期，Worker 每分钟清理。完成上传与释放预留在同一事务中，分片会话仅创建者可读写、完成或中止。迁移 `0005_upload_quotas` 后，旧的未完成分片会话需重新初始化；历史文件计入容量但不补计每日额度。API 每进程最多接收 8 个上传请求；Nginx 每客户端 IP 限制 8 个 API 连接，并流式转发请求体。相关正整数配置见 `.env.example`。

上传临时文件保存在专用 `.staging` 目录，活跃上传持有进程锁；Worker 不会清理仍被使用的文件，崩溃后的旧目录会在到期后回收。对象键在 MinIO 上传前写入预留记录；Worker 删除无文件记录引用的对象，并在对象删除成功后确认清理。删除失败或结果不确定时，相关容量继续被预留并在下次清理重试，避免靠存储故障绕过容量上限。此版本之前尚未完成的分片会话需重新初始化（返回 409），历史文件与引用保留。真实 PostgreSQL/MinIO 上传检查可运行 `RUN_UPLOAD_STORAGE_TESTS=1 SKYCLOUD_TEST_MINIO_IMAGE=sha256:<构建输出的镜像ID> python -m pytest tests/integration/test_upload_storage.py`（在 backend 目录执行，需要 Docker）；测试创建独立容器并清理自身资源，Redis 缓存和 RabbitMQ 通知仍使用测试替身。

MCP 内联文件上限为 8 MiB，Base64 解码前检查编码长度；MCP JSON 请求体默认上限 32 MiB、每进程最多 4 个并发 POST 请求。对应正整数设置为 `MCP_MAX_REQUEST_SIZE` 和 `MCP_REQUEST_CONCURRENCY`；调整请求体大小时需同步 Nginx `/mcp` 的 `client_max_body_size`。

REST 鉴权仅接受 `Authorization: Bearer <access-token>`，不再接受 URL 中的 `?token=` 或通用 token Cookie。前端文件下载与受保护 Markdown 图片通过带鉴权头的请求获取 Blob，组件销毁或内容切换时释放图片 URL；旧文件引用中的 JWT query 参数会在插入页面前被移除。外部 MCP 凭据仍通过 Authorization 请求头使用。

OpenCode 页面入口通过 `POST /api/workspace/{id}/opencode/ui-ticket` 获取 30 秒一次性票据，兑换最长 10 分钟的 HttpOnly 运行时会话；不再在 iframe URL 中发送 Web JWT。每个运行时必须使用独立主机名，以隔离脚本、浏览器存储和 Service Worker。默认本机入口为 `http://r{runtime_id}.skycloud.localhost:8081`；部署时设置 `APP_ORIGIN` 和 `RUNTIME_UI_ORIGIN_TEMPLATE`（例如 `https://r{runtime_id}.runtime.example.com`），并配置通配 DNS/TLS，将该域名转发到前端的 8081 监听端口。父应用的 `/runtime-ui` 被拒绝，8081 仅转发运行时网关。HTTPS 会话使用 `__Host-` Secure Cookie；运行时页面每个请求都会重新校验当前编辑权限、密码版本和容器身份。前端本地 Vite 开发需另行运行这个 UI 入口并将 `APP_ORIGIN` 设为开发页面的实际来源。

运行时入口与父页面必须具有相同的 scheme 和可注册域后缀，以支持 Strict SameSite Cookie；生产示例父页面为 `https://app.example.com`，运行时为 `https://r{runtime_id}.runtime.example.com`。本机父页面请使用 `http://app.skycloud.localhost`；Vite 开发时使用该主机名及实际开发端口。流式响应每 15 秒重新检查凭据，过期会关闭连接。

### 认证行为验证

在 `backend` 目录安装 `python -m pip install --require-hashes --only-binary=:all: -r requirements-test.lock`，再运行 `python -m pytest --ignore=tests/real`。刷新会话单元测试使用执行真实 Lua 脚本的 fakeredis；`RUN_REFRESH_SESSION_TESTS=1 python -m pytest tests/integration/test_refresh_sessions.py` 使用独立 Redis/PostgreSQL 容器验证并发重放与撤销，需要 Docker，并在结束后清理自己的资源。前端验证在 `frontend` 目录运行 `npm run test:unit` 和 `npm run build`。

Python 的运行、测试和 Controller 依赖分别锁定在 `backend/requirements.lock`、`backend/requirements-test.lock` 和 `backend/runtime_controller/requirements.lock`，包含所有传递依赖版本及发布文件的 SHA-256。测试锁以运行锁为约束，生产镜像不安装测试工具。输入文件仍是对应的 `requirements*.txt`；安装时使用 `.lock`。

更新依赖时，使用 uv 0.12.1 运行 `python scripts/lock-python.py --upgrade`；不加 `--upgrade` 会保留已有锁定版本。随后审计锁文件、运行上述测试，并运行 `python scripts/check-python-runtime.py`。该检查使用真实 SDK、合成凭证和进程内 HTTP，验证 API、MCP 协议/鉴权、代理 Host/Origin 及 Agent 构造，不访问部署环境或模型供应商。完整模型调用仍需单独集成验证。

Python、Node、nginx、PostgreSQL、Redis 和 RabbitMQ 的生产镜像已固定为支持 amd64/arm64 的镜像索引摘要；后端 apt 包使用签名 Debian 快照。MinIO 的原公共镜像地址已不可拉取，MinIO 由 `backend/minio_image/lock.json` 锁定的官方源码构建，校验源码 SHA-256，固定 Go 工具链和运行镜像，并使用上游 `go.sum` 校验全部 Go 依赖；不会自动下载另一个工具链或改写 `go.mod`。容器内 `/usr/share/minio/` 包含源码压缩包、许可证及锁文件。执行 `scripts/build-minio-image.py --env-file .env` 后，Compose 使用本机不可变 `MINIO_IMAGE`，换 Docker 主机时重新构建。

源码镜像的持久性验证在 `backend` 目录运行 `RUN_MINIO_IMAGE_TESTS=1 SKYCLOUD_TEST_MINIO_IMAGE=sha256:<构建输出的镜像ID> python -m pytest tests/integration/test_minio_image.py`。测试创建独立卷，验证读写、重启、删除及源码校验和；若设置 `SKYCLOUD_TEST_LEGACY_MINIO_IMAGE` 为本地已有的旧镜像 ID，还会验证旧数据卷升级。测试不下载旧镜像，未提供旧镜像时会明确跳过该用例。

工作空间 CRUD、成员管理与 Runtime Controller 路由，以及工作空间/成员角色依赖，使用 FastAPI 同步处理函数在线程池执行；异步鉴权显式将用户资料缓存/数据库读取及 MCP Token 查询交给线程池。`tests/test_workspace_threadpool.py` 验证这些同步操作等待期间其他 HTTP 请求仍可响应，并覆盖真实空间创建与请求字段校验。Assistant 流式处理及其他异步服务的同步 I/O 和 Session 使用仍需继续核查。

Assistant 会话 CRUD、状态查询、交接与权限回复也由线程池执行；流式请求的准备和取消的数据库事务及序列化显式移入线程池。权限回复成功提交后才通知流式任务，跨线程通知通过 Future 所属事件循环发送；提交失败不会触发执行，Future 已取消时忽略迟到通知。`tests/test_assistant_control_threads.py` 覆盖事务顺序与线程安全；真实专家网关集成测试使用独立线程和独立数据库 Session 回复权限，覆盖单次允许和拒绝。流内部的数据库操作仍需继续改造。

专家任务的 Runtime 检查、启动与已有 Runtime 的 MCP 凭据刷新使用工作线程，每个阶段创建并关闭独立数据库 Session，重新校验编辑权限，返回不可变的 Runtime ID/连接地址。它们不再与流式消息保存共用 Session。`tests/test_expert_runtime_threads.py` 验证慢 Controller 操作期间事件循环可响应及 Session 归属；真实专家网关测试同时覆盖冷启动与已有 Runtime 刷新。消息保存与权限轮询内部仍需继续改造。
