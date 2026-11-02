"""应用端 RPC 客户端. 不具备 Docker SDK 或 socket 的直接访问权."""

import os
import re
from typing import Final, Literal, cast
from urllib.parse import quote, urlsplit

import httpx
from runtime_common.contracts import ContainerPayload, UserSnapshotRow
from runtime_common.json_types import JsonObject, JsonValue

CONTROLLER_URL_ENV: Final = "RUNTIME_CONTROLLER_URL"
DEFAULT_CONTROLLER_URL: Final = "http://127.0.0.1:5003"
CONTROLLER_TIMEOUT_SECONDS: Final = 180
HTTP_BAD_REQUEST: Final = 400
HTTP_NOT_FOUND: Final = 404
HTTP_CONFLICT: Final = 409
GENERATION_PATTERN: Final = r"[0-9a-f]{32}"
CONTAINER_ID_PATTERN: Final = r"[0-9a-f]{64}"
AUTH_HEADER_NAME: Final = "X-Runtime-Controller-Token"


class ControllerError(RuntimeError):
    """Controller 拒绝或未能成功响应操作异常."""


class ControllerNotFound(ControllerError):
    """Controller 中不存在所请求的运行时实例."""


class ControllerGenerationMissing(ControllerError):
    """记录的运行时世代候选实例缺失异常."""

    def __init__(self, generation: str, container_id: str) -> None:
        """记录 Controller 无法找到的实例标识."""
        self.generation, self.container_id = generation, container_id
        super().__init__("Recorded runtime candidate is missing")




def controller_url() -> str:
    """获取配置的 Controller 基础 URL, 移除末尾斜杠."""
    return os.getenv(CONTROLLER_URL_ENV, DEFAULT_CONTROLLER_URL).rstrip("/")


def auth_headers(base_url: str | None = None) -> dict[str, str]:
    """返回 Controller 鉴权凭证, 除非目标 URL 超出 Controller 范围."""
    if base_url is not None:
        candidate, expected = urlsplit(base_url), urlsplit(controller_url())
        runtime_prefix = expected.path.rstrip("/") + "/runtimes/"
        if (candidate.scheme, candidate.netloc) != (
            expected.scheme,
            expected.netloc,
        ) or not candidate.path.startswith(runtime_prefix):
            return {}
    token = os.getenv("RUNTIME_CONTROLLER_TOKEN", "")
    if not token:
        message = "Runtime controller credential is not configured"
        raise ControllerError(message)
    return {AUTH_HEADER_NAME: token}


def runtime_url(ref: str) -> str:
    """返回单个运行时引用的固定 HTTP 中继 URL."""
    return controller_url() + "/runtimes/" + quote(ref, safe="") + "/http"



def _container_payloads(payload: JsonValue) -> list[UserSnapshotRow]:
    """将 Controller 的固定快照响应投影为其 RPC 数据模式."""
    return cast("list[UserSnapshotRow]", payload)


class ControllerClient:
    """用于独立 Runtime Controller 的轻量级 HTTP RPC 客户端."""

    def _call(self, method: str, path: str, *, json: JsonValue = None) -> JsonValue:
        """调用单个固定的 Controller RPC 并返回其 JSON 结果."""
        try:
            response = httpx.request(
                method,
                controller_url() + path,
                headers=auth_headers(),
                timeout=CONTROLLER_TIMEOUT_SECONDS,
                trust_env=False,
                follow_redirects=False,
                json=json,
            )
        except httpx.HTTPError as exc:
            message = "Runtime controller is unavailable"
            raise ControllerError(message) from exc
        if response.status_code == HTTP_NOT_FOUND:
            message = "Runtime not found"
            raise ControllerNotFound(message)
        if response.status_code == HTTP_CONFLICT and path == "/containers/prepare":
            self._raise_generation_missing(response, json)
        if response.status_code >= HTTP_BAD_REQUEST:
            message = "Runtime controller rejected operation"
            raise ControllerError(message)
        return response.json()

    @staticmethod
    def _raise_generation_missing(
        response: httpx.Response,
        request: JsonValue,
    ) -> None:
        """当响应体提供充分凭据时抛出精确的 409 标识错误."""
        requested = request.get("generation") if isinstance(request, dict) else None
        try:
            detail: JsonValue = response.json()
        except ValueError:
            return
        if not isinstance(detail, dict):
            return
        generation = detail.get("generation")
        container_id = detail.get("container_id")
        if (
            detail.get("code") == "generation_candidate_missing"
            and generation == requested
            and isinstance(generation, str)
            and re.fullmatch(GENERATION_PATTERN, generation)
            and isinstance(container_id, str)
            and re.fullmatch(CONTAINER_ID_PATTERN, container_id)
        ):
            raise ControllerGenerationMissing(generation, container_id)

    def inspect(self, ref: str) -> ContainerPayload:
        """返回单个托管容器的有界快照."""
        path = "/containers/" + quote(ref, safe="")
        return cast("ContainerPayload", self._call("GET", path))

    def prepare(
        self,
        workspace_id: int,
        user_id: int,
        runtime_id: int,
        generation: str,
    ) -> ContainerPayload:
        """幂等地准备一个运行时世代实例."""
        payload: JsonObject = {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "runtime_id": runtime_id,
            "generation": generation,
        }
        try:
            data = self._call("POST", "/containers/prepare", json=payload)
        except ControllerNotFound as exc:
            message = "Runtime generation preparation could not be confirmed"
            raise ControllerError(message) from exc
        return cast("ContainerPayload", data)

    def retire_generation(
        self,
        workspace_id: int,
        user_id: int,
        runtime_id: int,
        generation: str,
    ) -> None:
        """在移除前对单个运行时世代进行持久化退役标记."""
        payload: JsonObject = {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "runtime_id": runtime_id,
            "generation": generation,
        }
        try:
            self._call("POST", "/containers/retire-generation", json=payload)
        except ControllerNotFound as exc:
            message = "Runtime generation retirement could not be confirmed"
            raise ControllerError(message) from exc

    def remove_legacy(self, ref: str) -> None:
        """仅在 Controller 确认后移除历史容器."""
        path = "/retired-containers/" + quote(ref, safe="") + "/remove"
        try:
            self._call("POST", path)
        except ControllerNotFound as exc:
            # 旧版 Controller 在路由不支持或容器不存在时均返回 404.
            # 只有标准 inspect 路由能证明确实不存在; 重试该路由而非丢弃意图.
            # 保持错误语义清晰并向上传递确认失败.
            message = "Legacy cleanup outcome could not be confirmed"
            raise ControllerError(message) from exc

    def user_snapshot(self, user_id: int) -> list[UserSnapshotRow]:
        """返回为单个用户记录的所有托管运行时标识."""
        return _container_payloads(self._call("GET", f"/users/{user_id}/containers"))

    def action(
        self,
        container_id: str,
        action: Literal["start", "stop", "restart", "remove"],
    ) -> None:
        """对精确的容器标识执行指定固定操作."""
        path = "/containers/" + quote(container_id, safe="")
        self._call("POST", path + "/actions/" + action)

    def read_config(self, container_id: str) -> JsonObject:
        """返回单个容器的归一化配置信息."""
        path = "/containers/" + quote(container_id, safe="") + "/config"
        return cast("JsonObject", self._call("GET", path))

    def write_config(self, container_id: str, config: JsonObject) -> None:
        """更新替换单个容器的归一化配置信息."""
        path = "/containers/" + quote(container_id, safe="") + "/config"
        self._call("PUT", path, json=config)
