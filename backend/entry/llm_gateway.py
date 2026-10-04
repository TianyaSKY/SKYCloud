"""Runtime-facing model gateway. Exposes only scoped chat requests and health."""

import os
from contextlib import asynccontextmanager
from starlette.concurrency import run_in_threadpool

from fastapi import FastAPI
import uvicorn

from app.exceptions import register_exception_handlers
from app.features.runtime_llm.router import router

@asynccontextmanager
async def lifespan(app):
    from app.infra.extensions import engine
    from app.infra.schema import check_schema
    from app.infra.secrets import configured_password
    from app.features.auth.legacy_admin import check_legacy_admin
    configured_password("DATABASE_URL password", engine.url.password)
    await run_in_threadpool(check_schema, engine)
    await run_in_threadpool(check_legacy_admin, engine)
    yield


app = FastAPI(lifespan=lifespan, title="SKYCloud Runtime Model Gateway", docs_url=None, redoc_url=None, openapi_url=None)
register_exception_handlers(app)
app.include_router(router)


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("RUNTIME_LLM_GATEWAY_PORT", "5002")))
