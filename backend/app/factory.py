from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend
from redis import asyncio as aioredis

from app import initialize_application
from app.extensions import db, REDIS_HOST, REDIS_PORT
from app.exceptions import register_exception_handlers
from app.routers import auth, chat, file, folder, inbox, share, sys_dict, user


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_application()

    # Initialize FastAPI Cache with Redis
    redis = aioredis.from_url(
        f"redis://{REDIS_HOST}:{REDIS_PORT}", encoding="utf8", decode_responses=True
    )
    FastAPICache.init(RedisBackend(redis), prefix="fastapi-cache")

    yield
    db.remove_session()
    await redis.close()


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

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    return app
