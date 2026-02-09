"""
FastAPI 应用入口点
使用 uvicorn 运行：uvicorn run:app --reload --port 5000
"""

import os

import uvicorn

from app.factory import create_fastapi_app

app = create_fastapi_app()

if __name__ == "__main__":
    # 获取端口，默认为 5000
    port = int(os.environ.get("BACKEND_API_PORT", 5000))
    uvicorn.run("run:app", host="0.0.0.0", port=port, reload=True)
