"""FastAPI 应用工厂：组装路由、异常处理。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import initialize_application
from app.exceptions import register_exception_handlers
from app.features.auth.router import router as auth_router
from app.features.chat.router import router as chat_router
from app.features.file.router import router as file_router
from app.features.folder.router import router as folder_router
from app.features.inbox.router import router as inbox_router
from app.features.share.router import router as share_router
from app.features.sys_dict.router import router as sys_dict_router
from app.features.token_usage.router import router as token_usage_router
from app.features.workspace.router import router as workspace_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    """应用生命周期：启动初始化。"""
    initialize_application()
    try:
        yield
    finally:
        from app.features.chat.rerank import close_rerank_client
        from app.infra.llm.client import close_llm_clients

        await close_rerank_client()
        await close_llm_clients()


def create_fastapi_app() -> FastAPI:
    """创建并挂载全部 HTTP 路由。请求级 session 由 Depends(get_db) 管理。"""
    app = FastAPI(
        title="SKYCloud API",
        version="1.0.0",
        lifespan=lifespan,
    )
    register_exception_handlers(app)

    app.include_router(auth_router, prefix="/api")
    app.include_router(folder_router, prefix="/api")
    app.include_router(file_router, prefix="/api")
    app.include_router(sys_dict_router, prefix="/api")
    app.include_router(share_router, prefix="/api")
    app.include_router(inbox_router, prefix="/api")
    app.include_router(chat_router, prefix="/api")
    app.include_router(token_usage_router, prefix="/api")
    app.include_router(workspace_router, prefix="/api")

    @app.get("/api/health")
    def health():
        """进程存活探针，不依赖 DB。"""
        return {"status": "ok"}

    return app
