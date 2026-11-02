"""面向运行时的模型网关. 仅暴露作用域受限的聊天请求与健康检查."""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import uvicorn
from app.exceptions import register_exception_handlers
from app.features.auth.legacy_admin import check_legacy_admin
from app.features.runtime_llm.router import router
from app.infra.extensions import engine
from app.infra.schema import check_schema
from app.infra.secrets import configured_password
from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

_GATEWAY_PORT_ENV = "RUNTIME_LLM_GATEWAY_PORT"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    """校验数据库数据模式与废弃的历史管理员凭证."""
    configured_password("DATABASE_URL password", engine.url.password)
    await run_in_threadpool(check_schema, engine)
    await run_in_threadpool(check_legacy_admin, engine)
    yield


app = FastAPI(
    lifespan=lifespan,
    title="SKYCloud Runtime Model Gateway",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
register_exception_handlers(app)
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    """向运行时容器报告网关存活状态."""
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv(_GATEWAY_PORT_ENV, "5002")),
    )
