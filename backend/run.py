"""HTTP API 进程入口：创建 FastAPI 应用并由 uvicorn 托管。

本地开发：uvicorn run:app --reload --port 5000
或直接：python run.py
"""

import os

import uvicorn

from app.api.factory import create_fastapi_app

app = create_fastapi_app()

if __name__ == "__main__":
    # 默认端口 5000，可用 BACKEND_API_PORT 覆盖
    port = int(os.environ.get("BACKEND_API_PORT", 5000))
    uvicorn.run("run:app", host="0.0.0.0", port=port, reload=True)
