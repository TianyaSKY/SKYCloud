<div align="center">
  <h1 style="letter-spacing:4px;">SKYCLOUD</h1>
  <p><strong>AI 驱动的云文件管理平台</strong></p>
  <p>
    <img src="https://img.shields.io/badge/Python-3.10%2B-blue" alt="Python">
    <img src="https://img.shields.io/badge/Vue.js-3.x-4FC08D" alt="Vue">
    <img src="https://img.shields.io/badge/FastAPI-0.115-009688" alt="FastAPI">
    <img src="https://img.shields.io/badge/PostgreSQL-15-336791" alt="PostgreSQL">
    <img src="https://img.shields.io/badge/Docker-Enabled-2496ED" alt="Docker">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </p>
  <p><a href="./README_EN.md">English README</a></p>
</div>

# SKYCloud

SKYCloud 是一个 AI 增强的云文件管理系统，支持：
- 文件上传、预览与整理
- 分享链接与收件箱协作
- 基于 LangChain/OpenAI 的智能对话

## 功能截图

| 登录 / 控制台 | 文件浏览 |
| --- | --- |
| ![登录控制台](images/img.png) | ![文件浏览](images/img_1.png) |

| 智能助手 |
| --- |
| ![智能助手](images/img_2.png) |

## 技术栈

- 后端：FastAPI、SQLAlchemy、Redis、PostgreSQL (pgvector)
- 前端：Vue 3、TypeScript、Vite、Arco Design
- Worker：Python 多线程任务进程（异步索引/整理）
- 部署：Docker Compose

## 项目结构

```text
SKYCloud/
|- backend/                # FastAPI 应用 + worker
|  |- app/                 # 路由、模型、服务、Schemas
|  |- worker/              # 后台任务处理
|  |- run.py               # FastAPI 入口
|  |- tasks.py             # Worker 入口
|  `- requirements.txt
|- frontend/               # Vue 3 + Vite 应用
|  |- src/
|  `- package.json
|- images/                 # README 图片
|- docker-compose.yml
`- .env.example
```

## 环境要求

- Python 3.10+
- Node.js 20+
- Docker + Docker Compose（可选，推荐一键启动）

## 快速开始 (Docker)

1. 克隆并进入项目：

```bash
git clone https://github.com/TianyaSKY/SKYCloud
cd SKYCloud
```

2. 创建环境变量文件：

```bash
cp .env.example .env
```

3. 修改 `.env` 关键配置：
- `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_PORT`
- `REDIS_PORT`
- `BACKEND_API_PORT`
- `UPLOAD_HOST_PATH`（宿主机上传目录）
- `UPLOAD_FOLDER`（容器内上传目录，默认 `/data/uploads`）
- `WORKER_MAX_THREADS`
- `FRONTEND_PORT`
- `DEFAULT_MODEL_PWD`
- `SECRET_KEY`（生产环境务必设置）

4. 启动全部服务：

```bash
docker-compose up -d --build
```

5. 访问地址：
- 前端：`http://localhost:${FRONTEND_PORT}`（默认 `http://localhost:80`）
- 后端健康检查：`http://localhost:${BACKEND_API_PORT}/api/health`（默认 `http://localhost:5000/api/health`）

## 本地开发

### 1. 先准备后端依赖服务
先启动 PostgreSQL 和 Redis（本机或 Docker 均可）。

### 2. 启动后端 API

```bash
cd backend
pip install -r requirements.txt
python run.py
# or
uvicorn run:app --reload --port 5000
```

### 3. 启动后端 Worker

```bash
cd backend
python tasks.py
```

### 4. 启动前端

```bash
cd frontend
npm install
npm run dev
```

前端开发服务器会将 `/api` 请求代理到 `http://localhost:5000`。

## 构建与类型检查

```bash
cd frontend
npm run type-check
npm run build
```

## 默认账号

`backend/init_db.sql` 内包含默认管理员初始化数据：
- 用户名：`admin`
- 密码：`admin123`

为安全起见，非演示环境请首次登录后立即修改。

## 说明

- 当前仓库尚未建立完整自动化测试体系。
- 修改核心逻辑时建议补充测试（后端建议 `pytest`，前端建议 `vitest`）。
