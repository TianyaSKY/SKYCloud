"""Docker operations restricted to managed runtimes and fixed configuration paths."""

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

from runtime_controller.client import ContainerPayload, UserSnapshotRow
from runtime_controller.generation_journal import (
    GenerationJournal,
    GenerationRecord,
    JournalIdentityError,
)
from runtime_controller.json_types import JsonObject, JsonValue
from runtime_controller.policy import (
    CONFIG_PATH,
    POLICY_VERSION,
    RUNTIME_USER,
    DockerCreateOptions,
    DockerRunOptions,
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
    """The Controller could not complete a runtime operation."""


class MissingRuntime(ControllerError):
    """The requested managed runtime does not exist."""


class MissingGenerationCandidate(ControllerError):
    """A recorded runtime generation candidate is absent."""

    def __init__(self, generation: str, container_id: str) -> None:
        """Record the identity that the Controller could not find."""
        self.generation, self.container_id = generation, container_id
        super().__init__("Recorded generation candidate is missing")


class PolicyViolation(ControllerError):
    """The requested container or generation violates the fixed policy."""


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    """Validated runtime identity plus its generation label."""

    workspace_id: int
    user_id: int
    runtime_id: int
    generation: str

    @property
    def triple(self) -> tuple[int, int, int]:
        """Return the identity tuple stored in the generation journal."""
        return (self.workspace_id, self.user_id, self.runtime_id)


class ContainerListOptions(TypedDict):
    """Keyword options for the fixed Docker container list endpoint."""

    all: bool
    filters: dict[str, list[str]]


class ContainerApi(Protocol):
    """The fixed Docker API surface used to list visible containers."""

    def containers(
        self,
        **options: Unpack[ContainerListOptions],
    ) -> list[dict[str, Any]]:
        """Return one JSON row per visible container."""
        ...


class RuntimeContainers(Protocol):
    """The fixed Docker container collection surface used by this engine."""

    def run(self, **options: Unpack[DockerRunOptions]) -> "Container":
        """Create and start one container from the fixed policy options."""
        ...

    def create(self, **options: Unpack[DockerCreateOptions]) -> "Container":
        """Create one stopped container from the fixed policy options."""
        ...

    def get(self, key: str) -> "Container":
        """Return one container by reference."""
        ...


def _docker_error(name: str) -> type[Exception]:
    """Return the Docker SDK error class, or the local not-found fallback."""
    errors = getattr(docker, "errors", None)
    candidate = getattr(errors, name, None)
    if isinstance(candidate, type):
        return cast("type[Exception]", candidate)
    return MissingRuntime


def _runtime_containers(client: "docker.DockerClient") -> RuntimeContainers:
    """Project the SDK collection onto this engine's fixed policy surface.

    The published Docker stub restricts ``restart_policy`` to ``always`` and
    ``on-failure`` although the daemon also accepts the fixed ``no`` policy used
    for managed runtimes, so the collection is viewed through this exact surface.
    """
    return cast("RuntimeContainers", client.containers)


def _container_api(client: "docker.DockerClient") -> ContainerApi:
    """Project the SDK API client onto the typed container list surface."""
    return cast("ContainerApi", client.api)


def _list_containers(
    client: "docker.DockerClient", labels: list[str],
) -> list[dict[str, Any]]:
    """List containers through the fixed label filter."""
    api = _container_api(client)
    options = ContainerListOptions(all=True, filters={"label": labels})
    return api.containers(**options)


def _generation_label(labels: dict[str, str]) -> str | None:
    """Return the generation label of a managed container, if any."""
    return labels.get(GENERATION_LABEL)


def _version(labels: dict[str, str]) -> str | None:
    """Return the recorded runtime policy version, if any."""
    return labels.get(POLICY_LABEL)


class Engine:
    """Docker engine restricted to SKYCloud-managed runtime containers."""

    def __init__(
        self, settings: Settings, client: docker.DockerClient | None = None,
    ) -> None:
        """Bind the engine to its fixed policy and Docker daemon."""
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
        """Require the pinned runtime image to be present locally or pull it."""
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
        """Return a managed container, proving its identity and policy."""
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
        """Return only the fixed container fields the application may see."""
        container = self.managed(ref)
        labels: dict[str, str] = container.attrs["Config"]["Labels"]
        # Never expose environment variables or arbitrary Docker attributes.
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
        """Return the full container identity recorded by the daemon."""
        return cast("str", container.id)

    @staticmethod
    def _container_name(container: "Container") -> str:
        """Return the container name proven by :meth:`managed`."""
        return cast("str", container.name)

    def user_snapshot(self, user_id: int) -> list[UserSnapshotRow]:
        """One Docker list request, exposing only this user's managed identities."""
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

    def create(
        self, workspace_id: int, user_id: int, runtime_id: int,
    ) -> ContainerPayload:
        """Create one managed workspace runtime container."""
        network = self.client.networks.get(self.settings.network)
        if not network.attrs.get("Internal"):
            message = "Runtime network must be internal"
            raise PolicyViolation(message)
        self.ensure_image()
        name = container_name(workspace_id, user_id)
        try:
            existing: Container | None = self.managed(name)
        except MissingRuntime:
            existing = None
        if existing is not None:
            existing.remove(force=True, v=True)
        options = self.settings.container_options(workspace_id, user_id, runtime_id)
        container = self._containers().run(**options)
        container.reload()
        return self.inspect(self._container_id(container))

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
        """Idempotently prepare one runtime generation and record it ready."""
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
        """Prepare one generation while its journal row is locked."""
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
        """Commit the retirement fence, then remove the retired container."""
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
            # A daemon-accepted create can appear after retirement's
            # first absence observation, with no recorded full ID.
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
        """Retire generations recorded as retired but still visible."""
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
                # One deferred identity must never stop the reconciliation scan.
                logger.warning(
                    "Retired generation reconciliation deferred (%s)",
                    type(error).__name__,
                )
        return removed

    def _prepare_container(self, request: GenerationRequest) -> ContainerPayload:
        """Idempotently create a stopped generation without replacing old resources."""
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
        """Retry only transient absence after Docker reserved the name."""
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
        """Return the full identity of a freshly created container."""
        ref = container.id
        if not isinstance(ref, str):
            message = "Invalid runtime reference"
            raise PolicyViolation(message)
        return ref

    def _admit_prepared(self, container: "Container") -> ContainerPayload:
        """Reject an implicitly extended policy before admitting the container."""
        container.reload()
        try:
            ref = self._reference(container)
            self.managed(ref, require_policy=True)
        except PolicyViolation:
            # An image may declare implicit volumes. Reject the new resource
            # before admission and remove only the container just created.
            container.remove(force=True, v=True)
            raise
        return self.inspect(ref)

    def action(self, ref: str, action: str) -> None:
        """Apply one fixed lifecycle action to a managed container."""
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
        """Remove only an exact historical SKYCloud identity, never relay to it."""
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
        """Return the runtime's normalized configuration object."""
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
        """Replace the runtime's normalized configuration object."""
        container = self.managed(ref, require_policy=True)
        # Fixed commands only; both data and the generated temp path are safely
        # quoted. Caller input never becomes a command or filesystem path.
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
        """Return the fixed runtime URL and Basic credential for one container."""
        container = self.managed(ref, require_policy=True)
        if container.status != "running":
            message = "Runtime is not running"
            raise PolicyViolation(message)
        labels: dict[str, str] = container.attrs["Config"]["Labels"]
        # Read actual isolated network IP instead of caller-controlled URLs.
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

