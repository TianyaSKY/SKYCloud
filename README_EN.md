<div align="center">
  <h1>SKYCLOUD</h1>
  <p><strong>AI-powered cloud file management platform</strong></p>
  <p>
    <img src="https://img.shields.io/badge/Python-3.10%2B-blue" alt="Python">
    <img src="https://img.shields.io/badge/Vue.js-3.x-4FC08D" alt="Vue">
    <img src="https://img.shields.io/badge/FastAPI-0.115-009688" alt="FastAPI">
    <img src="https://img.shields.io/badge/PostgreSQL-15-336791" alt="PostgreSQL">
    <img src="https://img.shields.io/badge/Docker-Enabled-2496ED" alt="Docker">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </p>
  <p><a href="./README.md">Chinese</a></p>
</div>

---

SKYCloud is an AI-enhanced cloud file management system that provides an all-in-one experience from file storage and
intelligent retrieval to autonomous execution.

## Highlights

- **File management** - Uploads (chunked uploads / instant uploads), downloads, previews, batch operations, folder
  trees, and format conversion
- **AI chat (RAG)** - Multi-dimensional keyword rewriting -> Multi-Query vector recall -> RRF fusion -> optional
  reranking -> SSE streaming output, with image references
- **AI file organization** - LangGraph ReAct Agent automatic classification, incremental / full organization modes, and
  inbox notifications after organization
- **All-in-one workspace** - Isolated Docker sandboxes + MCP protocol, allowing AI Agents to read/write cloud files, run
  code, and automate tasks in a Manus-style workflow
- **MCP service** - 17 tools / 4 prompts / 2 resources, directly accessible from clients such as Claude Desktop and
  Cursor
- **Sharing & inbox** - Expiring share links and system notification delivery
- **Token usage tracking** - Consumption statistics for chat / indexing / organization, including an admin summary view
- **Performance optimizations** - Bloom Filter permission pre-checks, Redis caching, RabbitMQ async indexing, and
  parallel embedding

| AI Assistant Chat                           | File Preview                             |
|---------------------------------------------|------------------------------------------|
| ![AI Assistant Chat](images/agent_chat.png) | ![File Preview](images/file_preview.png) |

## Architecture

![System Architecture](images/architecture.png)

| Layer           | Technology                                                                  |
|-----------------|-----------------------------------------------------------------------------|
| Backend         | FastAPI · SQLAlchemy · Pydantic                                             |
| AI              | LangChain · LangGraph · OpenAI API (compatible with SiliconFlow and others) |
| Vector Database | PostgreSQL 15 + pgvector (1024 dimensions)                                  |
| Queue / Cache   | RabbitMQ 3.13 · Redis 7                                                     |
| Frontend        | Vue 3 · TypeScript · Vite · Arco Design                                     |
| MCP             | FastMCP (Streamable HTTP)                                                   |
| Deployment      | Docker Compose                                                              |

## Quick Start

> Requirements: Docker + Docker Compose

```bash
git clone https://github.com/TianyaSKY/SKYCloud && cd SKYCloud
cp .env.example .env
# Edit .env and at least configure API_URL / API_KEY / MODEL for the Chat and Embedding models
docker-compose up -d --build
```

After startup, visit `http://localhost` (default port 80). The default administrator account is `admin / admin123`.

> For non-demo environments, change the password immediately after the first login. If `SECRET_KEY` is not configured,
> an insecure default value is used. Always configure it in production.

### Stopping services (including OpenCode workspaces)

OpenCode workspaces are created dynamically with `docker run` by the backend and are **not** listed in
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

## RAG Retrieval Augmentation

![RAG Pipeline](images/rag_pipeline.png)

**Offline**: File upload -> RabbitMQ -> Worker generates descriptions + 1024-dimensional embeddings -> pgvector

**Online**: 6-dimensional keyword rewriting -> multi-query generation -> parallel vector recall -> RRF fusion ->
reranking -> SSE streaming output

## MCP Access

The MCP Server runs in a standalone container on the default port **5001**. Each user is auto-provisioned with a *
*single** MCP token: open the avatar menu → **MCP Token** to copy or refresh. Workspaces inject this token automatically
on create/start/restart — no manual "Connect MCP" step.

| Workspace / MCP        | Token Usage                            |
|------------------------|----------------------------------------|
| ![MCP](images/mcp.png) | ![Token Usage](images/token-usage.png) |

## All-In-One Workspace

Each workspace runs in an isolated Docker container. Through the MCP protocol, AI can autonomously execute tasks inside
the sandbox and interact with the cloud drive.

| Share Management           | Workspace                          |
|----------------------------|------------------------------------|
| ![Share](images/share.png) | ![Workspace](images/workspace.png) |

## Local Development

```bash
# Backend (requires PostgreSQL / Redis / RabbitMQ)
cd backend && pip install -r requirements.txt
python run.py          # API
python tasks.py        # Worker
python mcp_run.py      # MCP Server

# Frontend (Vite dev proxy -> localhost:5000)
cd frontend && npm ci && npm run dev
```

## Project Structure

```
SKYCloud/
├── backend/
│   ├── app/
│   │   ├── api/            # HTTP: routers / schemas / dependencies / factory
│   │   ├── mcp/            # MCP protocol adapter (tools / resources)
│   │   ├── workers/        # Async jobs: indexing / organize / convert
│   │   ├── services/       # Shared business logic (API / MCP / Worker)
│   │   ├── models/         # SQLAlchemy models
│   │   ├── infra/          # Cache / queue / upload / datetime helpers
│   │   ├── extensions.py   # DB / Redis infrastructure
│   │   └── exceptions.py   # Shared exceptions
│   ├── run.py              # API entry point
│   ├── tasks.py            # Worker entry point
│   └── mcp_run.py          # MCP entry point
├── frontend/src/
│   ├── api/                # Axios wrappers
│   ├── components/         # Shared components
│   ├── views/              # Pages
│   ├── hooks/              # Composition API
│   └── router/             # Routes
├── docker-compose.yml
└── .env.example
```

## License

MIT
