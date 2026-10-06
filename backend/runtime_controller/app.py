"""Authenticated, bounded RPC and HTTP relay; no general Docker or exec endpoint."""

import asyncio
import base64
import hmac
import logging
import os
import socket
import time
from typing import Literal

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from runtime_controller.engine import Engine, MissingRuntime, PolicyViolation
from runtime_controller.policy import Settings
from runtime_controller.socket_relay import direct_connect, pump, terminal_path, SocketSlots, MAX_MESSAGE
from urllib.parse import urlencode

MAX_BODY = 256 * 1024
MAX_RESPONSE = 64 * 1024 * 1024
RELAY_ROOTS = {"global", "event", "session", "permission", "question", "mcp", "config", "instance", "agent", "project", "path", "vcs", "file", "find", "experimental", "command", "log", "provider", "lsp", "formatter", "doc"}


class CreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: int = Field(gt=0, le=2**63 - 1)
    user_id: int = Field(gt=0, le=2**63 - 1)
    runtime_id: int = Field(gt=0, le=2**63 - 1)


class PrepareRequest(CreateRequest):
    generation: str = Field(pattern=r"^[0-9a-f]{32}$")


class AuthAndBodyLimit:
    def __init__(self, app, token):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict((key.lower(), value) for key, value in scope.get("headers", []))
        if not hmac.compare_digest(headers.get(b"x-runtime-controller-token", b""), self.token):
            await JSONResponse({"detail": "Controller authentication required"}, status_code=401)(scope, receive, send)
            return
        body = bytearray()
        try:
            while True:
                message = await asyncio.wait_for(receive(), timeout=10)
                if message["type"] == "http.disconnect":
                    return
                part = message.get("body", b"")
                if len(body) + len(part) > MAX_BODY:
                    await JSONResponse({"detail": "Controller request too large"}, status_code=413)(scope, receive, send)
                    return
                body.extend(part)
                if not message.get("more_body", False):
                    break
        except asyncio.TimeoutError:
            await JSONResponse({"detail": "Controller request timed out"}, status_code=408)(scope, receive, send)
            return
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(settings=None, engine=None):
    settings = settings or Settings.from_env()
    engine = engine or Engine(settings)
    socket_slots = SocketSlots()
    app = FastAPI(title="SKYCloud Runtime Controller", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(AuthAndBodyLimit, token=settings.token)

    @app.exception_handler(RequestValidationError)
    async def validation(_, exc):
        return JSONResponse({"detail": "Invalid controller request"}, status_code=422)

    @app.exception_handler(MissingRuntime)
    async def missing(_, exc):
        return JSONResponse({"detail": "Runtime not found"}, status_code=404)

    @app.exception_handler(PolicyViolation)
    async def forbidden(_, exc):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(Exception)
    async def error(_, exc):
        logging.exception("Runtime controller operation failed")
        return JSONResponse({"detail": "Runtime controller operation failed"}, status_code=500)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/image/ensure")
    def image_ensure():
        engine.ensure_image()
        return {"image": settings.image}

    @app.post("/containers/create")
    def create(payload: CreateRequest):
        return engine.create(**payload.model_dump())

    @app.post("/containers/prepare")
    def prepare(payload: PrepareRequest):
        return engine.prepare(**payload.model_dump())

    @app.post("/containers/retire-generation")
    def retire_generation(payload: PrepareRequest):
        engine.retire_generation(**payload.model_dump())
        return {"success": True}

    @app.get("/users/{user_id}/containers")
    def user_snapshot(user_id: int):
        if user_id <= 0 or user_id > 2**63 - 1:
            raise HTTPException(422, "Invalid user identity")
        return engine.user_snapshot(user_id)

    @app.get("/containers/{ref}")
    def inspect(ref: str):
        return engine.inspect(ref)

    @app.post("/containers/{ref}/actions/{action}")
    def action(ref: str, action: Literal["start", "stop", "restart", "remove"]):
        engine.action(ref, action)
        return {"success": True}

    @app.post("/retired-containers/{ref}/remove")
    def remove_legacy(ref: str):
        engine.remove_legacy(ref)
        return {"success": True}

    @app.get("/containers/{ref}/config")
    def read_config(ref: str):
        return engine.read_config(ref)

    @app.put("/containers/{ref}/config")
    def write_config(ref: str, payload: dict):
        engine.write_config(ref, payload)
        return {"success": True}

    @app.websocket("/runtimes/{ref}/ws/{path:path}")
    async def terminal(ref: str, path: str, websocket: WebSocket):
        if not hmac.compare_digest(websocket.headers.get("x-runtime-controller-token", "").encode(), settings.token.encode()):
            await websocket.close(code=1008)
            return
        if not terminal_path(path) or len(websocket.scope.get("query_string", b"")) > 4096 or any(key not in {"directory", "location[directory]", "workspace", "cursor", "ticket"}
                                          for key in websocket.query_params):
            await websocket.close(code=1008)
            return
        acquired = False
        try:
            target, auth = await run_in_threadpool(engine.target, ref)
            if not socket_slots.acquire(ref):
                await websocket.close(code=1013)
                return
            acquired = True
            url = target.replace("http://", "ws://", 1) + "/" + path
            query = urlencode(list(websocket.query_params.multi_items()))
            if query:
                url += "?" + query
            credential = base64.b64encode((auth[0] + ":" + auth[1]).encode()).decode()
            async def guard():
                current = await run_in_threadpool(engine.target, ref)
                if current != (target, auth):
                    raise HTTPException(401, "Runtime identity changed")
            async with direct_connect(url, {"Authorization": "Basic " + credential}) as upstream:
                await websocket.accept()
                await pump(websocket, upstream, seconds=600, guard=guard)
        except asyncio.CancelledError:
            raise
        except Exception:
            try:
                await websocket.close(code=1011)
            except RuntimeError:
                pass
        finally:
            if acquired:
                socket_slots.release(ref)

    @app.api_route("/runtimes/{ref}/ui/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    @app.api_route("/runtimes/{ref}/http/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    async def relay(ref: str, path: str, request: Request):
        is_ui = request.url.path.startswith("/runtimes/" + ref + "/ui/")
        if (path.startswith("/") or "\\" in path or ":" in path
                or any(part in {".", ".."} for part in path.split("/"))
                or (not is_ui and (not path or path.split("/", 1)[0] not in RELAY_ROOTS))):
            raise HTTPException(403, "Unsupported runtime HTTP path")
        target, auth = await run_in_threadpool(engine.target, ref)
        # Cold-start health requests must return promptly; leaving failed
        # probes open for 90 seconds interferes with readiness retries.
        timeout = httpx.Timeout(2, connect=1) if path == "global/health" else httpx.Timeout(90, connect=5)
        client = httpx.AsyncClient(auth=auth, timeout=timeout, trust_env=False, follow_redirects=False)
        response = None
        try:
            headers = {key: request.headers[key] for key in ("content-type", "accept") if key in request.headers}
            if is_ui and request.headers.get("x-opencode-ticket") == "1":
                headers["x-opencode-ticket"] = "1"
            upstream = client.build_request(request.method, target + "/" + path,
                                            params=list(request.query_params.multi_items()), content=await request.body(), headers=headers)
            response = await client.send(upstream, stream=True)
        except httpx.HTTPError as exc:
            logging.warning("Runtime relay transport failed: %s", type(exc).__name__)
            await client.aclose()
            raise HTTPException(502, "Runtime unavailable") from exc
        except BaseException:
            await client.aclose()
            raise
        is_event = "text/event-stream" in response.headers.get("content-type", "")
        if not is_event:
            try:
                output = bytearray()
                deadline = time.monotonic() + 120
                async for chunk in response.aiter_bytes():
                    if len(output) + len(chunk) > MAX_RESPONSE or time.monotonic() > deadline:
                        raise HTTPException(502, "Runtime response limit exceeded")
                    output.extend(chunk)
                return Response(bytes(output), status_code=response.status_code,
                                headers={"Content-Type": response.headers.get("content-type", "application/json")})
            except httpx.HTTPError as exc:
                raise HTTPException(502, "Runtime unavailable") from exc
            finally:
                await response.aclose()
                await client.aclose()

        async def events():
            total = 0
            deadline = time.monotonic() + 3600
            try:
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_RESPONSE or time.monotonic() > deadline:
                        break
                    yield chunk
            except httpx.HTTPError:
                return
            finally:
                await response.aclose()
                await client.aclose()

        return StreamingResponse(events(), status_code=response.status_code,
                                 media_type="text/event-stream", headers={"Cache-Control": "no-store"})

    return app


if __name__ == "__main__":
    import uvicorn

    # Compose binds only the private control interface. Runtime containers
    # cannot contact the RPC listener on its separate runtime interface.
    host = os.getenv("RUNTIME_CONTROLLER_BIND_HOST", "127.0.0.1")
    if host != "127.0.0.1":
        host = socket.gethostbyname(host)
    uvicorn.run(create_app(), host=host, port=int(os.getenv("RUNTIME_CONTROLLER_PORT", "5003")),
                ws_max_size=MAX_MESSAGE, ws_max_queue=4)
