"""Application-side RPC client. It has no Docker SDK or socket access."""

import os
import re
from typing import Final, TypedDict, cast
from urllib.parse import quote, urlsplit

import httpx

from runtime_controller.json_types import JsonObject, JsonValue

CONTROLLER_URL_ENV: Final = "RUNTIME_CONTROLLER_URL"
DEFAULT_CONTROLLER_URL: Final = "http://127.0.0.1:5003"
CONTROLLER_TIMEOUT_SECONDS: Final = 180
HTTP_BAD_REQUEST: Final = 400
HTTP_NOT_FOUND: Final = 404
HTTP_CONFLICT: Final = 409
GENERATION_PATTERN: Final = r"[0-9a-f]{32}"
CONTAINER_ID_PATTERN: Final = r"[0-9a-f]{64}"
SHORT_ID_LENGTH: Final = 12
AUTH_HEADER_NAME: Final = "X-Runtime-Controller-Token"


class ControllerError(RuntimeError):
    """The Controller rejected or could not answer an operation."""


class ControllerNotFound(ControllerError):
    """The Controller does not know the requested runtime."""


class ControllerGenerationMissing(ControllerError):
    """A recorded runtime generation candidate is absent."""

    def __init__(self, generation: str, container_id: str) -> None:
        """Record the identity that the Controller could not find."""
        self.generation, self.container_id = generation, container_id
        super().__init__("Recorded runtime candidate is missing")


class UserSnapshotRow(TypedDict):
    """One managed runtime identity returned for a single user."""

    id: str
    workspace_id: int
    user_id: int
    runtime_id: int
    status: str


class ImageRecord(TypedDict):
    """Fixed image identity returned by the ensure RPC."""

    image: str


class ContainerPayload(TypedDict):
    """Fixed container fields returned by the Controller RPC."""

    id: str
    name: str
    status: str
    image: str | None
    labels: dict[str, str]
    ports: dict[str, list[dict[str, str]] | None]


class RemoteContainerConfig(TypedDict):
    """Docker-style container config exposed to application callers."""

    Image: str | None
    Labels: dict[str, str] | None


class RemoteContainerNetworkSettings(TypedDict):
    """Docker-style network settings exposed to application callers."""

    Ports: dict[str, list[dict[str, str]] | None]


class RemoteContainerAttrs(TypedDict):
    """The subset of Docker container attributes the application reads."""

    Config: RemoteContainerConfig
    NetworkSettings: RemoteContainerNetworkSettings


def controller_url() -> str:
    """Return the configured Controller base URL without a trailing slash."""
    return os.getenv(CONTROLLER_URL_ENV, DEFAULT_CONTROLLER_URL).rstrip("/")


def auth_headers(base_url: str | None = None) -> dict[str, str]:
    """Return the Controller credential, unless the URL leaves Controller scope."""
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
    """Return the fixed HTTP relay URL for one runtime reference."""
    return controller_url() + "/runtimes/" + quote(ref, safe="") + "/http"


def _container_payload(payload: JsonValue) -> ContainerPayload:
    """Project the Controller's fixed container response onto its RPC schema."""
    return cast("ContainerPayload", payload)


def _container_payloads(payload: JsonValue) -> list[UserSnapshotRow]:
    """Project the Controller's fixed snapshot response onto its RPC schema."""
    return cast("list[UserSnapshotRow]", payload)


class ControllerClient:
    """Docker-like RPC surface for the standalone Controller."""

    def __init__(self) -> None:
        """Bind the container and image collections to this client."""
        self.containers = self
        self.images = _Images(self)

    def call(self, method: str, path: str, *, json: JsonValue = None) -> JsonValue:
        """Call one fixed Controller RPC and return its JSON result."""
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
        """Raise the precise 409 identity error when the payload proves it."""
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

    def get(self, ref: str) -> "RemoteContainer":
        """Return one managed runtime container by reference."""
        path = "/containers/" + quote(ref, safe="")
        return RemoteContainer(self, _container_payload(self.call("GET", path)))

    def prepare(
        self,
        workspace_id: int,
        user_id: int,
        runtime_id: int,
        generation: str,
    ) -> "RemoteContainer":
        """Idempotently prepare one runtime generation."""
        payload: JsonObject = {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "runtime_id": runtime_id,
            "generation": generation,
        }
        try:
            data = self.call("POST", "/containers/prepare", json=payload)
        except ControllerNotFound as exc:
            message = "Runtime generation preparation could not be confirmed"
            raise ControllerError(message) from exc
        return RemoteContainer(self, _container_payload(data))

    def retire_generation(
        self,
        workspace_id: int,
        user_id: int,
        runtime_id: int,
        generation: str,
    ) -> None:
        """Durably retire one runtime generation before removal."""
        payload: JsonObject = {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "runtime_id": runtime_id,
            "generation": generation,
        }
        try:
            self.call("POST", "/containers/retire-generation", json=payload)
        except ControllerNotFound as exc:
            message = "Runtime generation retirement could not be confirmed"
            raise ControllerError(message) from exc

    def remove_legacy(self, ref: str) -> None:
        """Remove one historical container only after the Controller confirms it."""
        path = "/retired-containers/" + quote(ref, safe="") + "/remove"
        try:
            self.call("POST", path)
        except ControllerNotFound as exc:
            # Old Controllers return 404 for an unsupported route as well as
            # absent containers. Only the established inspect route can prove
            # absence; retry there instead of discarding an unconfirmed intent.
            message = "Legacy cleanup outcome could not be confirmed"
            raise ControllerError(message) from exc

    def user_snapshot(self, user_id: int) -> list[UserSnapshotRow]:
        """Return every managed runtime identity recorded for one user."""
        return _container_payloads(self.call("GET", f"/users/{user_id}/containers"))

    def create(
        self,
        workspace_id: int,
        user_id: int,
        runtime_id: int,
    ) -> "RemoteContainer":
        """Create one managed workspace runtime container."""
        payload: JsonObject = {
            "workspace_id": workspace_id,
            "user_id": user_id,
            "runtime_id": runtime_id,
        }
        data = self.call("POST", "/containers/create", json=payload)
        return RemoteContainer(self, _container_payload(data))


class _Images:
    """Fixed image identity checks; no caller-selected image or pull control."""

    def __init__(self, client: ControllerClient) -> None:
        """Bind the image collection to its owning client."""
        self.client = client

    def get(self, ref: str) -> ImageRecord:
        """Ensure the pinned runtime image exists and verify its identity."""
        record = cast("ImageRecord", self.client.call("POST", "/image/ensure"))
        if record["image"] != ref:
            message = "Runtime image differs from controller policy"
            raise ControllerError(message)
        return record

    pull = get


class RemoteContainer:
    """Docker-like view of one runtime container over the fixed RPC."""

    def __init__(self, client: ControllerClient, data: ContainerPayload) -> None:
        """Project the Controller's container response onto Docker-like fields."""
        self.client = client
        self.id = data["id"]
        self.short_id = self.id[:SHORT_ID_LENGTH]
        self.name = data["name"]
        self.status = data["status"]
        self.attrs: RemoteContainerAttrs = {
            "Config": {"Image": data["image"], "Labels": data["labels"]},
            "NetworkSettings": {"Ports": data["ports"]},
        }

    def _path(self, suffix: str = "") -> str:
        return "/containers/" + self.id + suffix

    def _action(self, action: str) -> None:
        self.client.call("POST", self._path("/actions/" + action))

    def start(self) -> None:
        """Start this runtime container."""
        self._action("start")

    def stop(self) -> None:
        """Stop this runtime container."""
        self._action("stop")

    def restart(self) -> None:
        """Restart this runtime container."""
        self._action("restart")

    def remove(self) -> None:
        """Remove this runtime container."""
        self._action("remove")

    def read_config(self) -> JsonObject:
        """Return a copy of the runtime's normalized configuration object."""
        return cast("JsonObject", self.client.call("GET", self._path("/config")))

    def write_config(self, config: JsonObject) -> None:
        """Replace the runtime's normalized configuration object."""
        self.client.call("PUT", self._path("/config"), json=config)
