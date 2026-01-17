<div align="center">
  <h1 style="letter-spacing:4px;">SKYCLOUD</h1>
  <p><strong>智能云端 · 高效存储 · AI 赋能</strong></p>
  <p>
    <img src="https://img.shields.io/badge/Python-3.10%2B-blue" alt="Python">
    <img src="https://img.shields.io/badge/Vue.js-3.x-4FC08D" alt="Vue">
    <img src="https://img.shields.io/badge/Flask-2.x-000000" alt="Flask">
    <img src="https://img.shields.io/badge/PostgreSQL-15-336791" alt="PostgreSQL">
    <img src="https://img.shields.io/badge/Docker-Enabled-2496ED" alt="Docker">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </p>
</div>

---

SKYCloud 是一个集成了 AI 能力的智能云平台，专注于文档处理与管理。它利用大语言模型（LLMs）和向量数据库，提供语义搜索、智能文档交互及自动化整理等高级功能。

## ✨ 功能特性

*   **🤖 AI 智能分析**: 集成 LangChain 和 OpenAI，支持文档自动索引、语义搜索及分类整理。
*   **🔍 向量检索**: 基于 PostgreSQL 和 `pgvector` 扩展，实现高效的文档内容语义匹配。
*   **📄 多格式预览**: 内置支持 PDF、Word、Excel 等多种办公文档的在线直接预览。
*   **🎨 现代化 UI**: 采用 Vue 3 + Vite + Arco Design 构建，提供极致的响应式交互体验。
*   **🐳 容器化部署**: 全量支持 Docker Compose，一键搭建开发与生产环境。

## 📸 项目截图

| 首页概览 | 登录界面 |
| :---: | :---: |
| ![首页](images/img.png) | ![登录界面](images/img_1.png) |

| 智能文件整理 |
| :---: |
| ![问答界面](images/img_2.png) |

## 📂 项目结构

```text
SKYCloud/
├── backend/            # Flask 后端服务
│   ├── app/            # 核心业务逻辑 (API, Models, Services)
│   ├── worker/         # 异步任务处理 (Celery/Tasks)
│   └── Dockerfile      # 后端镜像构建
├── frontend/           # Vue 3 前端应用
│   ├── src/            # 源代码
│   └── Dockerfile      # 前端镜像构建
├── images/             # 项目文档图片
├── docker-compose.yml  # 容器编排配置
└── .env.example        # 环境变量模板
```

## 🛠️ 技术栈

### 后端 (Backend)
- **核心框架**: Flask, Flask-RESTX
- **AI 引擎**: LangChain, LangGraph, OpenAI API
- **数据库**: PostgreSQL (pgvector), Redis
- **ORM**: SQLAlchemy
- **文档解析**: PyMuPDF, python-docx, pdf2image

### 前端 (Frontend)
- **核心框架**: Vue 3, Vite, TypeScript
- **UI 组件**: Arco Design Vue
- **文档预览**: @vue-office

## 🚀 快速开始

### 前置要求
- Docker & Docker Compose
- (可选) Python 3.10+, Node.js 20+

### 快速启动 (推荐)

1.  **克隆仓库**:
    ```bash
    git clone https://github.com/TianyaSKY/SKYCloud.git
    cd SKYCloud
    ```

2.  **配置环境**:
    ```bash
    cp .env.example .env
    # 根据需要修改 .env 中的配置
    ```

3.  **一键启动**:
    ```bash
    docker-compose up -d
    ```

4.  **访问应用**:
    - 前端地址: `http://localhost`

## ⚙️ 配置说明

### 环境变量
请确保在 `.env` 文件中正确配置以下关键变量：
- `DATABASE_URL`: PostgreSQL 连接字符串。
- `UPLOAD_FOLDER`: 文件上传存储路径（容器内默认为 `/upload`）。

### 启用 AI 功能
1. 使用默认管理员账户登录。
2. 进入 **系统管理 -> 字典配置**。
3. 配置相关的 AI 模型参数（如 API Key、Base URL 等）。

## 🔐 默认账户

系统初始化后提供的管理员账户：
- **用户名**: `admin`
- **密码**: `admin123`

> [!IMPORTANT]
> **安全提示**: 默认账户仅用于系统初始化。为了数据安全，建议首次登录后立即创建个人账户，并避免在生产环境直接使用 `admin` 账户进行日常操作。

## 📄 开源协议

本项目基于 [MIT License](LICENSE) 协议开源。
