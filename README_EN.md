# SKYCloud

SKYCloud is an AI-enabled cloud file management system with:
- File upload, preview, and organization
- Share links and inbox-style collaboration
- LLM chat capabilities based on LangChain/OpenAI

## Screenshots

| Login / Dashboard | File Browser |
| --- | --- |
| ![Login Dashboard](images/img.png) | ![File Browser](images/img_1.png) |

| Chat Assistant |
| --- |
| ![Chat Assistant](images/img_2.png) |

## Tech Stack

- Backend: FastAPI, SQLAlchemy, Redis, PostgreSQL (pgvector)
- Frontend: Vue 3, TypeScript, Vite, Arco Design
- Worker: Python multi-thread worker for async file indexing/organizing
- Deployment: Docker Compose

## Repository Structure

```text
SKYCloud/
|- backend/                # FastAPI app + worker
|  |- app/                 # Routers, models, services, schemas
|  |- worker/              # Background task handlers
|  |- run.py               # FastAPI entry
|  |- tasks.py             # Worker entry
|  `- requirements.txt
|- frontend/               # Vue 3 + Vite app
|  |- src/
|  `- package.json
|- images/                 # README images
|- docker-compose.yml
`- .env.example
```

## Prerequisites

- Python 3.10+
- Node.js 20+
- Docker + Docker Compose (optional, recommended for one-command startup)

## Quick Start (Docker)

1. Clone and enter project:

```bash
git clone https://github.com/TianyaSKY/SKYCloud
cd SKYCloud
```

2. Create environment file:

```bash
cp .env.example .env
```

3. Update key variables in `.env`:
- `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_PORT`
- `REDIS_PORT`
- `BACKEND_API_PORT`
- `UPLOAD_HOST_PATH` (host path for uploaded files)
- `WORKER_MAX_THREADS`
- `FRONTEND_PORT`
- `DEFAULT_MODEL_PWD`
- `CHAT_API_URL`, `CHAT_API_KEY`, `CHAT_API_MODEL`
- `EMB_API_URL`, `EMB_API_KEY`, `EMB_MODEL_NAME`
- `VL_API_URL`, `VL_API_KEY`, `VL_API_MODEL`
- `RERANK_API_URL`, `RERANK_API_KEY`, `RERANK_MODEL`, `RERANK_TOP_K`
- `RAG_VECTOR_FETCH_K`
- `SECRET_KEY` (strongly recommended in production)

Note: the container upload directory is now fixed to `/data/uploads`, so `UPLOAD_FOLDER` is no longer an environment variable.

Model configuration is now loaded from environment variables and is no longer stored in `sys_dict`.
RAG now supports an optional rerank model (vector recall first, then reranking).

4. Start all services:

```bash
docker-compose up -d --build
```

5. Access:
- Frontend: `http://localhost:${FRONTEND_PORT}` (default `http://localhost:80`)
- Backend health: `http://localhost:${BACKEND_API_PORT}/api/health` (default `http://localhost:5000/api/health`)

## Local Development

### 1. Start backend dependencies

Start PostgreSQL + Redis first (locally or via Docker).

### 2. Run backend API

```bash
cd backend
pip install -r requirements.txt
python run.py
# or
uvicorn run:app --reload --port 5000
```

### 3. Run backend worker

```bash
cd backend
python tasks.py
```

### 4. Run frontend

```bash
cd frontend
npm install
npm run dev
```

Frontend dev server proxies `/api` requests to `http://localhost:5000`.

## Build & Type Check

```bash
cd frontend
npm run type-check
npm run build
```

## Default Account

`backend/init_db.sql` contains default admin initialization data:
- Username: `admin`
- Password: `admin123`

For security, change it after first login in non-demo environments.

## Notes

- There are currently no established automated tests in this repository.
- If you modify core logic, add tests (recommended: `pytest` for backend, `vitest` for frontend).
- Current `docker-compose.yml` uses `python app.py` for `backend-api`, but the backend entry file is `backend/run.py`. If API container fails to start, update compose command to `python run.py`.
