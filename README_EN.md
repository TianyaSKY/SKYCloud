<div align="center">
  <h1>SKYCLOUD</h1>
  <p><strong>AI-powered cloud file management platform</strong></p>
  <p>
    <img src="https://img.shields.io/badge/Python-3.11%2B-blue" alt="Python">
    <img src="https://img.shields.io/badge/Vue.js-3.x-4FC08D" alt="Vue">
    <img src="https://img.shields.io/badge/FastAPI-0.115-009688" alt="FastAPI">
    <img src="https://img.shields.io/badge/PostgreSQL-15-336791" alt="PostgreSQL">
    <img src="https://img.shields.io/badge/Docker-Enabled-2496ED" alt="Docker">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </p>
  <p><a href="./README.md">Chinese</a></p>
</div>

---

SKYCloud combines a cloud drive, retrieval-augmented Q&A and an AI that can act on its own: upload and preview files,
search your data in natural language, let AI organize the drive, and run code inside a per-workspace Docker sandbox.

## Features

- **File management** — Uploads (chunked / instant), downloads, previews, batch operations, folder trees, format
  conversion
- **AI chat (RAG)** — Multi-query vector recall → RRF fusion → optional rerank → SSE streaming output, with image
  references
- **Dual-mode assistant** — Quick mode reads the current workspace; expert mode drives a controlled OpenCode Runtime
  with tool calls, permission prompts, cancellation and diffs
- **AI file organization** — LangGraph ReAct agent classification, incremental / full modes, inbox notification when
  organization finishes
- **All-in-one workspace** — Isolated Docker sandbox plus MCP, so AI can read and write the drive, run code and
  automate tasks
- **MCP service** — 16 tools / 4 prompts / 2 resources, directly usable from Claude Desktop, Cursor and other clients
- **Sharing & inbox** — Expiring share links and system notifications
- **Token usage** — Consumption statistics for chat / indexing / organization, including an admin summary view

## Screenshots

| AI assistant chat                           | File preview                             |
|---------------------------------------------|------------------------------------------|
| ![AI assistant chat](images/agent_chat.png) | ![File preview](images/file_preview.png) |

| Share management           | Workspace                          | Token usage                            |
|----------------------------|------------------------------------|----------------------------------------|
| ![Share](images/share.png) | ![Workspace](images/workspace.png) | ![Token usage](images/token-usage.png) |

## Architecture

![System architecture](images/architecture.png)

| Layer           | Technology                              |
|-----------------|-----------------------------------------|
| Backend         | FastAPI · SQLAlchemy · Pydantic         |
| AI              | LangChain · LangGraph · OpenAI-compatible API |
| Vector store    | PostgreSQL 15 + pgvector (1024 dimensions) |
| Queue / cache   | RabbitMQ 3.13 · Redis 7                 |
| Frontend        | Vue 3 · TypeScript · Vite · Arco Design |
| MCP             | FastMCP (Streamable HTTP)               |
| Deployment      | Docker Compose                          |

## Quick Start

Requires Python 3.11+, Docker and Docker Compose; runtime hosts may be amd64 or arm64.

```bash
git clone https://github.com/TianyaSKY/SKYCloud && cd SKYCloud
python3 scripts/generate-env.py
python3 scripts/build-runtime-image.py --env-file .env
python3 scripts/build-minio-image.py --env-file .env
# Edit .env and at least configure API_URL / API_KEY / MODEL for the Chat and Embedding models (CHAT_* / EMB_*)
docker compose up -d --build
docker compose exec backend-api python -m entry.bootstrap_admin --username admin
```

Then open `http://app.skycloud.localhost` (default port 80). The administrator is created interactively by the command
above.

Both build scripts verify locked artifacts, produce host-local immutable image IDs and write them back to `.env`; rerun
them when moving to another Docker host. See [deployment](docs/en/deployment.md) for secrets, database upgrades and
shutdown.

## Documentation

| Document                                                    | Contents                                                      |
|-------------------------------------------------------------|---------------------------------------------------------------|
| [Deployment and operations](docs/en/deployment.md)          | Image builds, secrets and admins, database upgrades, shutdown |
| [Security model](docs/en/security.md)                       | Runtime isolation, auth and sessions, permissions, MCP credentials |
| [AI capabilities](docs/en/ai.md)                            | RAG retrieval, dual-mode assistant, MCP access, workspaces    |
| [Operating conventions](docs/en/operations.md)              | Uploads and quotas, background jobs, time zones, API and logging |
| [Local development](docs/en/development.md)                 | Running locally, tests, dependency locks and CI               |

Chinese documentation: [README.md](README.md) and [docs/](docs/).

## Project Structure

```
SKYCloud/
├── backend/
│   ├── app/
│   │   ├── api/                # HTTP entry points: auth, dependencies, router assembly
│   │   ├── features/           # Domain modules (file / folder / share / chat / assistant / workspace …)
│   │   ├── mcp/                # MCP protocol adapter: tools, resources, auth, audit
│   │   ├── infra/              # Queue, cache, uploads, indexing, model calls
│   │   ├── models/             # SQLAlchemy models
│   │   └── exceptions.py       # Shared exceptions
│   ├── entry/                  # Process entry points: API / Worker / MCP / migrate / admin commands
│   ├── migrations/             # Alembic migrations
│   ├── runtime_controller/     # Runtime container controller (standalone service)
│   ├── runtime_image/          # Expert Runtime sandbox image
│   └── tests/                  # Unit and integration tests
├── frontend/src/               # Vue 3 app (api / components / views / stores / router)
├── scripts/                    # Env generation, image builds, dependency locking
├── docs/                       # Chinese documentation (docs/en for English)
├── docker-compose.yml
└── .env.example
```

## License

MIT
