from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app import initialize_application
from app.exceptions import register_exception_handlers
from app.extensions import db
from app.api.routers import auth, chat, file, folder, inbox, share, sys_dict, token_usage, user, workspace


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_application()
    yield
    db.remove_session()


def create_fastapi_app() -> FastAPI:
    app = FastAPI(
        title="SKYCloud API",
        version="1.0.0",
        lifespan=lifespan,
    )
    register_exception_handlers(app)

    @app.middleware("http")
    async def db_session_middleware(request: Request, call_next):
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
        return {"status": "ok"}

    return app
