"""FastAPI 应用工厂: 组装路由分发与全局异常处理."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import initialize_application
from app.api.contracts import (
    HealthResponse,
    SuccessEnvelope,
    SuccessResponse,
    success_payload,
)
from app.exceptions import register_exception_handlers
from app.features.assistant.router import router as assistant_router
from app.features.auth.router import router as auth_router
from app.features.chat.rerank import close_rerank_client
from app.features.file.router import router as file_router
from app.features.folder.router import router as folder_router
from app.features.inbox.router import router as inbox_router
from app.features.share.router import router as share_router
from app.features.sys_dict.router import router as sys_dict_router
from app.features.token_usage.router import router as token_usage_router
from app.features.workspace.router import router as workspace_router
from app.features.workspace.ui_gateway import router as ui_gateway_router
from app.infra.llm.client import close_llm_clients
from app.infra.upload_limits import UploadBodyLimitMiddleware


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    """执行启动阶段校验并在关闭时清理相关资源."""
    initialize_application()
    try:
        yield
    finally:
        await close_rerank_client()
        await close_llm_clients()


def create_fastapi_app() -> FastAPI:
    """创建 FastAPI 实例并挂载所有 HTTP 业务路由."""
    app = FastAPI(
        title="SKYCloud API",
        version="1.0.0",
        lifespan=lifespan,
    )
    register_exception_handlers(app)
    app.add_middleware(UploadBodyLimitMiddleware)

    app.include_router(auth_router, prefix="/api")
    app.include_router(folder_router, prefix="/api")
    app.include_router(file_router, prefix="/api")
    app.include_router(sys_dict_router, prefix="/api")
    app.include_router(share_router, prefix="/api")
    app.include_router(inbox_router, prefix="/api")
    app.include_router(token_usage_router, prefix="/api")
    app.include_router(workspace_router, prefix="/api")
    app.include_router(ui_gateway_router)
    app.include_router(assistant_router, prefix="/api")

    @app.get("/api/health", response_model=SuccessResponse[HealthResponse])
    def health() -> SuccessEnvelope[HealthResponse]:
        """报告进程存活状态, 无需连接数据库."""
        return success_payload(HealthResponse(status="ok"))

    return app
