"""FastAPI 应用工厂：组装路由、异常处理与请求级 SQLAlchemy session 清理。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app import initialize_application
from app.api.routers import auth, chat, file, folder, inbox, share, sys_dict, token_usage, user, workspace
from app.exceptions import register_exception_handlers
from app.extensions import db


@asynccontextmanager
async def lifespan(_: FastAPI):
    """应用生命周期：启动初始化，关闭时移除进程级 session。"""
    initialize_application()
    yield
    db.remove_session()


def create_fastapi_app() -> FastAPI:
    """创建并挂载全部 HTTP 路由；中间件保证每请求结束后清理 scoped_session。"""
    app = FastAPI(
        title="SKYCloud API",
        version="1.0.0",
        lifespan=lifespan,
    )
    register_exception_handlers(app)

    @app.middleware("http")
    async def db_session_middleware(request: Request, call_next):
        # scoped_session 按线程绑定；请求结束必须 remove，否则连接泄漏
        try:
            return await call_next(request)
        finally:
            db.session.remove()

    app.include_router(auth.router, prefix="/api")
    app.include_router(user.router, prefix="/api")
    app.include_router(folder.router, prefix="/api")
    app.include_router(file.router, prefix="/api")
    app.include_router(sys_dict.router, prefix="/api")
    app.include_router(share.router, prefix="/api")
    app.include_router(inbox.router, prefix="/api")
    app.include_router(chat.router, prefix="/api")
    app.include_router(token_usage.router, prefix="/api")
    app.include_router(workspace.router, prefix="/api")

    @app.get("/api/health")
    def health():
        """进程存活探针，不依赖 DB。"""
        return {"status": "ok"}

    return app
