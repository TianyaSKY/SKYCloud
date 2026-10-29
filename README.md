<div align="center">
  <h1>SKYCLOUD</h1>
  <p><strong>AI 驱动的云文件管理平台</strong></p>
  <p>
    <img src="https://img.shields.io/badge/Python-3.11%2B-blue" alt="Python">
    <img src="https://img.shields.io/badge/Vue.js-3.x-4FC08D" alt="Vue">
    <img src="https://img.shields.io/badge/FastAPI-0.115-009688" alt="FastAPI">
    <img src="https://img.shields.io/badge/PostgreSQL-15-336791" alt="PostgreSQL">
    <img src="https://img.shields.io/badge/Docker-Enabled-2496ED" alt="Docker">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </p>
  <p><a href="./README_EN.md">English</a></p>
</div>

---

SKYCloud 把云盘、检索问答和可自主执行的 AI 放在同一套系统里：上传与预览文件、用自然语言检索资料、让 AI 整理云盘，并在每个工作空间独立的 Docker 沙箱中运行代码。

## 功能

- **文件管理** — 上传（分片 / 秒传）、下载、预览、批量操作、文件夹树、格式转换
- **AI 对话（RAG）** — 多查询向量召回 → RRF 融合 → 可选 Rerank → SSE 流式输出，支持图片引用
- **双模式助手** — 快速模式只读当前工作空间资料；专家模式驱动受控 OpenCode Runtime，支持工具调用、权限确认、取消与 Diff
- **AI 文件整理** — LangGraph ReAct Agent 自动分类，增量 / 全量模式，整理后收件箱通知
- **全能工作区** — 独立 Docker 沙箱 + MCP 协议，AI 可读写云盘、运行代码、自动化任务
- **MCP 服务** — 16 个工具 / 4 个 Prompt / 2 个 Resource，Claude Desktop、Cursor 等客户端直接接入
- **分享 & 收件箱** — 带过期时间的分享链接与系统通知
- **Token 用量** — 对话 / 索引 / 整理的消耗统计，含管理员汇总视图

## 界面

| 智能助手对话                           | 文件预览                             |
|----------------------------------|----------------------------------|
| ![智能助手对话](images/agent_chat.png) | ![文件预览](images/file_preview.png) |

| 分享管理                    | 工作区                          | Token 用量                            |
|-------------------------|------------------------------|-------------------------------------|
| ![分享](images/share.png) | ![工作区](images/workspace.png) | ![Token 用量](images/token-usage.png) |

## 架构

![系统架构](images/architecture.png)

| 层级      | 技术                                         |
|---------|--------------------------------------------|
| 后端      | FastAPI · SQLAlchemy · Pydantic            |
| AI      | LangChain · LangGraph · OpenAI 兼容 API       |
| 向量库     | PostgreSQL 15 + pgvector（1024 维）           |
| 队列 / 缓存 | RabbitMQ 3.13 · Redis 7                    |
| 前端      | Vue 3 · TypeScript · Vite · Arco Design    |
| MCP     | FastMCP（Streamable HTTP）                   |
| 部署      | Docker Compose                             |

## 快速开始

需要 Python 3.11+、Docker 与 Docker Compose，运行时支持 amd64 和 arm64。

```bash
git clone https://github.com/TianyaSKY/SKYCloud && cd SKYCloud
python3 scripts/generate-env.py
python3 scripts/build-runtime-image.py --env-file .env
python3 scripts/build-minio-image.py --env-file .env
# 编辑 .env，至少填写 Chat 和 Embedding 的 API_URL / API_KEY / MODEL（CHAT_* / EMB_*）
docker compose up -d --build
docker compose exec backend-api python -m entry.bootstrap_admin --username admin
```

启动后访问 `http://app.skycloud.localhost`（默认端口 80）。管理员由上面的命令在终端交互式创建。

两个构建脚本会校验锁定产物、生成本机不可变镜像 ID 并写回 `.env`，换 Docker 主机后需重新执行。密钥、数据库升级、停机清理等部署细节见[部署与运维](docs/deployment.md)。

## 文档

| 文档                             | 内容                              |
|--------------------------------|---------------------------------|
| [部署与运维](docs/deployment.md)     | 镜像构建、密钥与管理员、数据库升级、停止服务          |
| [安全模型](docs/security.md)        | 运行时隔离、认证与会话、权限模型、MCP 凭据与审计      |
| [AI 能力](docs/ai.md)             | RAG 检索、双模式助手、MCP 接入、全能工作区       |
| [运行约定](docs/operations.md)      | 上传与配额、后台任务与清理、时间与时区、接口与日志约定     |
| [本地开发](docs/development.md)     | 本地运行、测试、依赖锁定与 CI                |

英文文档：[README_EN.md](README_EN.md)、[docs/en/](docs/en/)。

## 项目结构

```
SKYCloud/
├── backend/
│   ├── app/
│   │   ├── api/                # HTTP 入口：认证、依赖、路由装配
│   │   ├── features/           # 领域模块（file / folder / share / chat / assistant / workspace …）
│   │   ├── mcp/                # MCP 协议适配：工具、资源、鉴权、审计
│   │   ├── infra/              # 队列、缓存、上传、索引、模型调用
│   │   ├── models/             # SQLAlchemy 模型
│   │   └── exceptions.py       # 统一异常
│   ├── entry/                  # 进程入口：API / Worker / MCP / migrate / 管理员命令
│   ├── migrations/             # Alembic 迁移
│   ├── runtime_controller/     # 运行时容器控制器（独立服务）
│   ├── runtime_image/          # 专家 Runtime 沙箱镜像
│   └── tests/                  # 单元与集成测试
├── frontend/src/               # Vue 3 应用（api / components / views / stores / router）
├── scripts/                    # 环境生成、镜像构建、依赖锁定
├── docs/                       # 详细文档
├── docker-compose.yml
└── .env.example
```

## License

MIT
