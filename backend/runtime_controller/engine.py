"""限制在受管运行时与固定配置路径内的 Docker 操作引擎."""

import base64
import contextlib
import ipaddress
import json
import logging
import os
import re
import shlex
import time
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol, TypedDict, Unpack, cast

import docker
from runtime_common.contracts import ContainerPayload, UserSnapshotRow
from runtime_common.json_types import JsonObject, JsonValue

from runtime_controller.generation_journal import (
    GenerationJournal,
    GenerationRecord,
    JournalIdentityError,
)
from runtime_controller.policy import (
    CONFIG_PATH,
    POLICY_VERSION,
    RUNTIME_USER,
    DockerCreateOptions,
    Settings,
    container_create_options,
    container_name,
    generation_name,
    matches_runtime_name,
)

if TYPE_CHECKING:
    from docker.models.containers import Container

logger = logging.getLogger(__name__)

MAX_IDENTITY: Final = 2**63 - 1
CONTAINER_ID_PATTERN: Final = r"[0-9a-f]{64}"
SHORT_ID_PATTERN: Final = r"(?:[0-9a-f]{12,64})"
RUNTIME_NAME_PATTERN: Final = (
    r"skycloud-opencode-w[1-9][0-9]*-u[1-9][0-9]*(?:-g[0-9a-f]{32})?"
)
RUNTIME_REFERENCE_PATTERN: Final = (
    "(?:" + SHORT_ID_PATTERN + "|" + RUNTIME_NAME_PATTERN + ")"
)
IDENTITY_LABELS: Final = ("workspace_id", "user_id", "runtime_id")
GENERATION_LABEL: Final = "skycloud.generation"
COMPONENT_LABEL: Final = "skycloud.component"
RUNTIME_COMPONENT: Final = "opencode-runtime"
POLICY_LABEL: Final = "skycloud.runtime_policy"
INSPECT_LABELS: Final = frozenset(
    {
        COMPONENT_LABEL,
        "skycloud.workspace_id",
        "skycloud.user_id",
        "skycloud.runtime_id",
        POLICY_LABEL,
        GENERATION_LABEL,
    },
)
LOCAL_IMAGE_PATTERN: Final = "sha256:"
STOP_TIMEOUT_SECONDS: Final = 10
RESTART_TIMEOUT_SECONDS: Final = 3
CONFLICT_STATUS: Final = 409
CREATE_RETRY_SECONDS: Final = 2
CREATE_RETRY_DELAY_SECONDS: Final = 0.05
DEFAULT_SCAN_LIMIT: Final = 20
OPENCODE_PORT: Final = 3000


class ControllerError(RuntimeError):
    """Controller 无法完成运行时操作异常."""


class MissingRuntime(ControllerError):
    """请求的受管运行时不存在异常."""


class MissingGenerationCandidate(ControllerError):
    """记录的运行时世代候选实例缺失异常."""

    def __init__(self, generation: str, container_id: str) -> None:
        """记录 Controller 无法找到的实例标识."""
        self.generation, self.container_id = generation, container_id
        super().__init__("Recorded generation candidate is missing")


class PolicyViolation(ControllerError):
    """请求的容器或世代违反固定安全策略异常."""


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """经过校验的运行时标识及其世代标签."""

    workspace_id: int
    user_id: int
    runtime_id: int
    generation: str

    @property
    def triple(self) -> tuple[int, int, int]:
        """返回世代日志中持久化存储的三元组标识."""
        return (self.workspace_id, self.user_id, self.runtime_id)


class ContainerListOptions(TypedDict):
    """固定 Docker 容器列表端点的关键字选项."""

    all: bool
    filters: dict[str, list[str]]


class ContainerApi(Protocol):
    """列出可见容器所使用的固定 Docker API 表面."""

    def containers(
        self,
        **options: Unpack[ContainerListOptions],
    ) -> list[dict[str, Any]]:
        """为每个可见容器返回一行 JSON 字典."""
        ...


class RuntimeContainers(Protocol):
    """本引擎使用的固定 Docker 容器集合接口表面."""

    def create(self, **options: Unpack[DockerCreateOptions]) -> "Container":
        """根据固定策略选项创建一个已停止的容器."""
        ...

    def get(self, key: str) -> "Container":
        """根据引用获取单个容器."""
        ...


def _docker_error(name: str) -> type[Exception]:
    """返回 Docker SDK 异常类型, 或本地未找到回退类型."""
    errors = getattr(docker, "errors", None)
    candidate = getattr(errors, name, None)
    if isinstance(candidate, type):
        return cast("type[Exception]", candidate)
    return MissingRuntime


def _runtime_containers(client: "docker.DockerClient") -> RuntimeContainers:
    """将 SDK 集合映射到本引擎的固定策略接口表面.

    发布版 Docker stub 将 restart_policy 限制为 always 和
    on-failure, 尽管守护进程也接受受管运行时所用的固定 no 策略,
    因此本集合通过该精确接口进行访问。
    """
    return cast("RuntimeContainers", client.containers)


def _container_api(client: "docker.DockerClient") -> ContainerApi:
    """将 SDK API 客户端映射为类型化容器列表接口表面."""
    return cast("ContainerApi", client.api)


def _list_containers(
    client: "docker.DockerClient", labels: list[str],
) -> list[dict[str, Any]]:
    """通过固定标签过滤器列出容器."""
    api = _container_api(client)
    options = ContainerListOptions(all=True, filters={"label": labels})
    return api.containers(**options)


def _generation_label(labels: dict[str, str]) -> str | None:
    """返回受管容器的世代标签, 若不存在则返回 None."""
    return labels.get(GENERATION_LABEL)


def _version(labels: dict[str, str]) -> str | None:
    """返回记录的运行时策略版本, 若不存在则返回 None."""
    return labels.get(POLICY_LABEL)


class Engine:
    """限定于 SKYCloud 受管运行时容器的 Docker 引擎."""

    def __init__(
        self, settings: Settings, client: docker.DockerClient | None = None,
    ) -> None:
        """将引擎绑定至其固定策略与 Docker 守护进程."""
        self.settings = settings
        host = os.getenv("DOCKER_HOST")
        self.client = (
            client
            if client is not None
            else (docker.DockerClient(base_url=host) if host else docker.from_env())
        )

    def _containers(self) -> RuntimeContainers:
        return _runtime_containers(self.client)

    def ensure_image(self) -> None:
        """要求本地存在固定的运行时镜像, 否则执行拉取."""
        try:
            self.client.images.get(self.settings.image)
        except _docker_error("ImageNotFound") as exc:
            if self.settings.image.startswith(LOCAL_IMAGE_PATTERN):
                message = (
                    "Local runtime image is missing; "
                    "rebuild with scripts/build-runtime-image.py"
                )
                raise ControllerError(message) from exc
            self.client.images.pull(self.settings.image)

    def managed(
        self, ref: str, *, require_policy: bool = False,
    ) -> "Container":
        """返回受管容器, 并核验其标识与策略符合规范."""
        if not re.fullmatch(RUNTIME_REFERENCE_PATTERN, ref):
            message = "Invalid runtime reference"
            raise PolicyViolation(message)
        try:
            container = self._containers().get(ref)
        except _docker_error("NotFound") as exc:
            message = "Runtime not found"
            raise MissingRuntime(message) from exc
        labels: dict[str, str] = (
            container.attrs.get("Config", {}).get("Labels", {}) or {}
        )
        try:
            ids = [int(labels[f"skycloud.{key}"]) for key in IDENTITY_LABELS]
        except (KeyError, ValueError, TypeError) as exc:
            message = "Container is not a managed runtime"
            raise PolicyViolation(message) from exc
        name = container.name
        if (
            name is None
            or min(ids) <= 0
            or labels.get(COMPONENT_LABEL) != RUNTIME_COMPONENT
            or not matches_runtime_name(name, ids[0], ids[1], labels)
        ):
            message = "Container identity does not match runtime labels"
            raise PolicyViolation(message)
        if require_policy:
            self._require_current_policy(container, labels)
        return container

    def _require_current_policy(
        self, container: "Container", labels: dict[str, str],
    ) -> None:
        host: dict[str, Any] = container.attrs.get("HostConfig", {})
        networks: dict[str, Any] = (
            container.attrs.get("NetworkSettings", {}).get("Networks", {})
        )
        config: dict[str, Any] = container.attrs["Config"]
        if (
            _version(labels) != POLICY_VERSION
            or config.get("Image") != self.settings.image
            or config.get("User") != RUNTIME_USER
            or not host.get("ReadonlyRootfs")
            or set(host.get("CapDrop", [])) != {"ALL"}
            or "no-new-privileges:true" not in host.get("SecurityOpt", [])
            or host.get("Privileged")
            or host.get("Binds")
            or container.attrs.get("Mounts")
            or set(networks) != {self.settings.network}
        ):
            message = "Runtime requires recreation with the current policy"
            raise PolicyViolation(message)

    def inspect(self, ref: str) -> ContainerPayload:
        """仅返回应用程序允许查看的固定容器字段."""
        container = self.managed(ref)
        labels: dict[str, str] = container.attrs["Config"]["Labels"]
        # 绝不暴露环境变量或任意 Docker 内部属性。
        return {
            "id": self._container_id(container),
            "name": self._container_name(container),
            "status": container.status,
            "image": container.attrs["Config"].get("Image"),
            "labels": {
                key: value for key, value in labels.items() if key in INSPECT_LABELS
            },
            "ports": container.attrs.get("NetworkSettings", {}).get("Ports", {}),
        }

    @staticmethod
    def _container_id(container: "Container") -> str:
        """返回守护进程记录的完整容器标识."""
        return cast("str", container.id)

    @staticmethod
    def _container_name(container: "Container") -> str:
        """返回经 managed 核验通过的容器名称."""
        return cast("str", container.name)

    def user_snapshot(self, user_id: int) -> list[UserSnapshotRow]:
        """发起单次 Docker 列表请求, 仅暴露属于该用户的受管标识."""
        rows = _list_containers(
            self.client,
            [
                f"{COMPONENT_LABEL}={RUNTIME_COMPONENT}",
                f"skycloud.user_id={user_id}",
            ],
        )
        result: list[UserSnapshotRow] = []
        for row in rows:
            labels: dict[str, str] = row.get("Labels") or {}
            try:
                workspace_id = int(labels["skycloud.workspace_id"])
                owner_id = int(labels["skycloud.user_id"])
                runtime_id = int(labels["skycloud.runtime_id"])
            except (KeyError, ValueError, TypeError):
                continue
            if (
                min(workspace_id, owner_id, runtime_id) <= 0
                or owner_id != user_id
                or labels.get(COMPONENT_LABEL) != RUNTIME_COMPONENT
                or not any(
                    matches_runtime_name(
                        name.lstrip("/"), workspace_id, owner_id, labels,
                    )
                    for name in row.get("Names", [])
                )
                or not re.fullmatch(CONTAINER_ID_PATTERN, row.get("Id", ""))
            ):
                continue
            result.append(
                UserSnapshotRow(
                    id=row["Id"],
                    workspace_id=workspace_id,
                    user_id=owner_id,
                    runtime_id=runtime_id,
                    status="running" if row.get("State") == "running" else "stopped",
                ),
            )
        return result


    def _generation_record(
        self, request: GenerationRequest, *, nonblocking: bool = False,
    ) -> AbstractContextManager[GenerationRecord]:
        if (
            min(request.workspace_id, request.user_id, request.runtime_id) <= 0
            or max(request.workspace_id, request.user_id, request.runtime_id)
            > MAX_IDENTITY
        ):
            message = "Invalid runtime identity"
            raise PolicyViolation(message)
        try:
            generation_name(request.workspace_id, request.user_id, request.generation)
        except (ValueError, TypeError) as exc:
            message = "Invalid runtime generation"
            raise PolicyViolation(message) from exc
        journal = GenerationJournal(self.settings.state_path)
        return journal.locked(
            request.generation, request.triple, nonblocking=nonblocking,
        )

    def prepare(
        self, workspace_id: int, user_id: int, runtime_id: int, generation: str,
    ) -> ContainerPayload:
        """幂等地准备一个运行时世代并记录其为就绪状态."""
        request = GenerationRequest(workspace_id, user_id, runtime_id, generation)
        try:
            with self._generation_record(request) as record:
                return self._prepare_locked(record, request)
        except JournalIdentityError as exc:
            message = str(exc)
            raise PolicyViolation(message) from exc

    def _prepare_locked(
        self, record: GenerationRecord, request: GenerationRequest,
    ) -> ContainerPayload:
        """在世代日志行已锁定的状态下准备单个世代."""
        state = record.read()
        if state["state"] == "retired":
            message = "Runtime generation is retired"
            raise PolicyViolation(message)
        if state["container_id"]:
            container = self._managed_candidate(
                state["container_id"], request.generation,
            )
            self._require_generation_identity(container, request, "Recorded")
            return self.inspect(cast("str", container.id))
        result = self._prepare_container(request)
        record.ready(result["id"])
        return result

    def _managed_candidate(self, container_id: str, generation: str) -> "Container":
        try:
            return self.managed(container_id, require_policy=True)
        except MissingRuntime as error:
            raise MissingGenerationCandidate(generation, container_id) from error

    def _require_generation_identity(
        self, container: "Container", request: GenerationRequest, prefix: str,
    ) -> None:
        labels: dict[str, str] = container.attrs["Config"]["Labels"]
        if (
            labels.get(GENERATION_LABEL) != request.generation
            or tuple(int(labels[f"skycloud.{key}"]) for key in IDENTITY_LABELS)
            != request.triple
        ):
            message = f"{prefix} generation identity does not match"
            raise PolicyViolation(message)

    def retire_generation(
        self,
        workspace_id: int,
        user_id: int,
        runtime_id: int,
        generation: str,
        *,
        _nonblocking: bool = False,
    ) -> None:
        """提交退役栅栏, 随后移除该已退役容器."""
        request = GenerationRequest(workspace_id, user_id, runtime_id, generation)
        try:
            with self._generation_record(
                request, nonblocking=_nonblocking,
            ) as record:
                self._retire_locked(record, request)
        except JournalIdentityError as exc:
            message = str(exc)
            raise PolicyViolation(message) from exc

    def _retire_locked(
        self, record: GenerationRecord, request: GenerationRequest,
    ) -> None:
        record.retire()
        state = record.read()
        ref = state["container_id"] or generation_name(
            request.workspace_id, request.user_id, request.generation,
        )
        try:
            container = self.managed(ref)
        except MissingRuntime:
            if not state["container_id"]:
                return
            # 守护进程接受的创建可能在退役首次观测到缺失后才出现,
            # 且没有记录的完整 ID。
            try:
                container = self.managed(
                    generation_name(
                        request.workspace_id, request.user_id, request.generation,
                    ),
                )
            except MissingRuntime:
                return
        self._require_generation_identity(container, request, "Retired")
        try:
            container.remove(force=True, v=True)
        except _docker_error("NotFound"):
            pass

    def reconcile_retired(self, limit: int = DEFAULT_SCAN_LIMIT) -> int:
        """退役并清理已记录为退役但依然可见的世代容器."""
        rows = _list_containers(
            self.client, [f"{COMPONENT_LABEL}={RUNTIME_COMPONENT}"],
        )
        identities = GenerationJournal(self.settings.state_path).retired_candidates(
            rows, limit,
        )
        removed = 0
        for identity in identities:
            try:
                self.retire_generation(*identity, _nonblocking=True)
                removed += 1
            except Exception as error:
                # 单个推迟处理的实例绝不能中断整体对账扫描流程。
                logger.warning(
                    "Retired generation reconciliation deferred (%s)",
                    type(error).__name__,
                )
        return removed

    def _prepare_container(self, request: GenerationRequest) -> ContainerPayload:
        """幂等地创建已停止的世代容器, 不替换已有资源."""
        if (
            min(request.workspace_id, request.user_id, request.runtime_id) <= 0
            or max(request.workspace_id, request.user_id, request.runtime_id)
            > MAX_IDENTITY
        ):
            message = "Invalid runtime identity"
            raise PolicyViolation(message)
        try:
            name = generation_name(
                request.workspace_id, request.user_id, request.generation,
            )
        except (ValueError, TypeError) as exc:
            message = "Invalid runtime generation"
            raise PolicyViolation(message) from exc
        network = self.client.networks.get(self.settings.network)
        if not network.attrs.get("Internal"):
            message = "Runtime network must be internal"
            raise PolicyViolation(message)
        self.ensure_image()
        existing: ContainerPayload | None = None
        with contextlib.suppress(MissingRuntime):
            existing = self._existing_generation(request, name)
        if existing is not None:
            return existing
        options = container_create_options(
            self.settings.container_options(
                request.workspace_id, request.user_id, request.runtime_id,
            ),
            name=name,
            generation=request.generation,
        )
        try:
            container = self._containers().create(**options)
        except _docker_error("APIError") as exc:
            if getattr(exc, "status_code", None) != CONFLICT_STATUS:
                raise
            return self._await_name_reservation(request, name)
        return self._admit_prepared(container)

    def _existing_generation(
        self, request: GenerationRequest, name: str,
    ) -> ContainerPayload:
        container = self.managed(name, require_policy=True)
        labels: dict[str, str] = container.attrs["Config"]["Labels"]
        if int(labels["skycloud.runtime_id"]) != request.runtime_id:
            message = "Generation belongs to another runtime"
            raise PolicyViolation(message)
        return self.inspect(cast("str", container.id))

    def _await_name_reservation(
        self, request: GenerationRequest, name: str,
    ) -> ContainerPayload:
        """在 Docker 预留容器名称后仅针对偶发短暂缺失进行重试."""
        deadline = time.monotonic() + CREATE_RETRY_SECONDS
        while True:
            try:
                return self._existing_generation(request, name)
            except MissingRuntime:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(CREATE_RETRY_DELAY_SECONDS)

    @staticmethod
    def _reference(container: "Container") -> str:
        """返回新创建容器的完整标识."""
        ref = container.id
        if not isinstance(ref, str):
            message = "Invalid runtime reference"
            raise PolicyViolation(message)
        return ref

    def _admit_prepared(self, container: "Container") -> ContainerPayload:
        """在准入容器前拒绝任何隐式扩展的安全策略."""
        container.reload()
        try:
            ref = self._reference(container)
            self.managed(ref, require_policy=True)
        except PolicyViolation:
            # 镜像可能声明隐式卷。在准入前拒绝该新资源,
            # 且仅移除刚创建的容器。
            container.remove(force=True, v=True)
            raise
        return self.inspect(ref)

    def action(self, ref: str, action: str) -> None:
        """对受管容器执行单项固定的生命周期操作."""
        container = self.managed(ref, require_policy=action != "remove")
        labels: dict[str, str] = container.attrs["Config"]["Labels"]
        generation = _generation_label(labels)
        if action == "remove" and generation:
            self.retire_generation(
                *self._identity(labels, generation),
            )
            return
        if action in ("start", "restart") and generation:
            request = self._request(labels, generation)
            with self._generation_record(request) as record:
                state = record.read()
                if state["state"] != "ready" or state["container_id"] != container.id:
                    message = "Runtime generation is not active"
                    raise PolicyViolation(message)
                if action == "start":
                    container.start()
                else:
                    container.restart(timeout=RESTART_TIMEOUT_SECONDS)
            return
        if action == "start":
            container.start()
        elif action == "stop":
            container.stop(timeout=STOP_TIMEOUT_SECONDS)
        elif action == "restart":
            container.restart(timeout=RESTART_TIMEOUT_SECONDS)
        elif action == "remove":
            container.remove(force=True, v=True)
        else:
            message = "Unsupported runtime action"
            raise PolicyViolation(message)

    @staticmethod
    def _request(labels: dict[str, str], generation: str) -> GenerationRequest:
        return GenerationRequest(
            int(labels["skycloud.workspace_id"]),
            int(labels["skycloud.user_id"]),
            int(labels["skycloud.runtime_id"]),
            generation,
        )

    def _identity(
        self, labels: dict[str, str], generation: str,
    ) -> tuple[int, int, int, str]:
        request = self._request(labels, generation)
        return (*request.triple, request.generation)

    def remove_legacy(self, ref: str) -> None:
        """仅移除历史 SKYCloud 精确标识的容器, 绝不中继流量."""
        if not re.fullmatch(CONTAINER_ID_PATTERN, ref):
            message = "Legacy cleanup requires a full container ID"
            raise PolicyViolation(message)
        try:
            container = self._containers().get(ref)
        except _docker_error("NotFound") as exc:
            message = "Runtime not found"
            raise MissingRuntime(message) from exc
        labels: dict[str, str] = (
            container.attrs.get("Config", {}).get("Labels", {}) or {}
        )
        try:
            workspace = int(labels["skycloud.workspace_id"])
            user = int(labels["skycloud.user_id"])
        except (KeyError, ValueError, TypeError) as exc:
            message = "Container is not a historical SKYCloud runtime"
            raise PolicyViolation(message) from exc
        if (
            container.id != ref
            or min(workspace, user) <= 0
            or max(workspace, user) > MAX_IDENTITY
            or labels.get(COMPONENT_LABEL) != RUNTIME_COMPONENT
            or labels.get("skycloud.runtime_id") not in ("", "0")
            or container.name
            not in (
                f"skycloud-workspace-{workspace}",
                container_name(workspace, user),
            )
        ):
            message = "Historical container identity does not match its labels"
            raise PolicyViolation(message)
        try:
            container.remove(force=True, v=True)
        except _docker_error("NotFound") as exc:
            message = "Runtime not found"
            raise MissingRuntime(message) from exc

    def read_config(self, ref: str) -> JsonObject:
        """返回运行时归一化配置对象."""
        container = self.managed(ref, require_policy=True)
        result = container.exec_run(["cat", CONFIG_PATH], user=RUNTIME_USER)
        if result.exit_code != 0:
            return {}
        output = result.output
        if not isinstance(output, bytes | str):
            return {}
        try:
            parsed: JsonValue = json.loads(output)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def write_config(self, ref: str, config: JsonObject) -> None:
        """更新替换运行时归一化配置对象."""
        container = self.managed(ref, require_policy=True)
        # 仅使用固定命令; 数据和生成的临时路径均经过安全转义。
        # 调用方输入绝不会变为命令或文件系统路径。
        encoded = base64.b64encode(
            json.dumps(config, ensure_ascii=False).encode(),
        ).decode()
        temporary = CONFIG_PATH + ".tmp." + uuid.uuid4().hex
        command = (
            "umask 077; mkdir -p /home/opencode/.config/opencode && "
            f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(temporary)} "
            f"&& mv {shlex.quote(temporary)} {shlex.quote(CONFIG_PATH)}"
        )
        result = container.exec_run(["sh", "-c", command], user=RUNTIME_USER)
        if result.exit_code != 0:
            message = "Runtime configuration write failed"
            raise ControllerError(message)

    def target(self, ref: str) -> tuple[str, tuple[str, str]]:
        """返回指定容器的固定运行时 URL 与 Basic 鉴权凭证."""
        container = self.managed(ref, require_policy=True)
        if container.status != "running":
            message = "Runtime is not running"
            raise PolicyViolation(message)
        labels: dict[str, str] = container.attrs["Config"]["Labels"]
        # 读取实际隔离网络的容器 IP, 而非调用方控制的 URL。
        address = container.attrs["NetworkSettings"]["Networks"][
            self.settings.network
        ]["IPAddress"]
        if not address:
            message = "Runtime has no network address"
            raise PolicyViolation(message)
        address = str(ipaddress.IPv4Address(address))
        password = self.settings.password(
            int(labels["skycloud.runtime_id"]),
            int(labels["skycloud.user_id"]),
            int(labels["skycloud.workspace_id"]),
        )
        return f"http://{address}:{OPENCODE_PORT}", (self.settings.username, password)

