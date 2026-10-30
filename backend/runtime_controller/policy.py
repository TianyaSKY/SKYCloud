"""Fixed container policy; callers cannot supply Docker flags, mounts or commands."""

import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from typing import Final, Literal, TypedDict

CONFIG_PATH: Final = "/home/opencode/.config/opencode/opencode.json"
RUNTIME_USER: Final = "10001:10001"
POLICY_VERSION: Final = "4"

MIN_SECRET_LENGTH: Final = 32
MIN_CPU_QUOTA: Final = 10000
MAX_CPU_QUOTA: Final = 400000
DEFAULT_CPU_QUOTA: Final = "100000"
DEV_SECRETS: Final[frozenset[str]] = frozenset({"sky_cloud_secret_key_dev_only"})
PLACEHOLDER_MARKER: Final = "replace-with"

IMAGE_PATTERN: Final = r"(?:[a-zA-Z0-9./:_-]+@)?sha256:[0-9a-f]{64}"
MEMORY_PATTERN: Final = (
    r"(?:[1-4]g|(?:[2-9][0-9]{2}|[1-3][0-9]{3}|40[0-8][0-9]|409[0-6])m)"
)
GENERATION_PATTERN: Final = r"[0-9a-f]{32}"


class RestartPolicy(TypedDict):
    """Restart policy for managed runtimes; they never restart on their own."""

    Name: Literal["no"]


class ContainerEnvironment(TypedDict):
    """Fixed runtime environment; no caller-supplied variables."""

    HOME: str
    TMPDIR: str
    XDG_CONFIG_HOME: str
    XDG_CACHE_HOME: str
    XDG_DATA_HOME: str
    SKYCLOUD_WORKSPACE_ID: str
    SKYCLOUD_USER_ID: str
    SKYCLOUD_RUNTIME_ID: str
    OPENCODE_SERVER_USERNAME: str
    OPENCODE_SERVER_PASSWORD: str


class DockerRunOptions(TypedDict):
    """Fixed keyword arguments for ``containers.run``; no caller input."""

    image: str
    name: str
    detach: Literal[True]
    command: list[str]
    user: str
    working_dir: str
    read_only: bool
    cap_drop: list[str]
    security_opt: list[str]
    pids_limit: int
    mem_limit: str
    cpu_quota: int
    cpu_period: int
    network: str
    restart_policy: RestartPolicy
    tmpfs: dict[str, str]
    environment: ContainerEnvironment
    labels: dict[str, str]


class DockerCreateOptions(TypedDict):
    """Fixed keyword arguments for ``containers.create``; no ``detach``."""

    image: str
    name: str
    command: list[str]
    user: str
    working_dir: str
    read_only: bool
    cap_drop: list[str]
    security_opt: list[str]
    pids_limit: int
    mem_limit: str
    cpu_quota: int
    cpu_period: int
    network: str
    restart_policy: RestartPolicy
    tmpfs: dict[str, str]
    environment: ContainerEnvironment
    labels: dict[str, str]


def secret_setting(name: str) -> str:
    """Read one deployment secret, rejecting placeholders and short values."""
    value = os.getenv(name, "")
    if (
        len(value) < MIN_SECRET_LENGTH
        or PLACEHOLDER_MARKER in value.lower()
        or value in DEV_SECRETS
    ):
        message = (
            f"{name} must contain a distinct random secret of at least 32 characters"
        )
        raise ValueError(message)
    return value


@dataclass(frozen=True)
class Settings:
    """Fixed Controller and runtime container policy."""

    token: str
    server_secret: str
    image: str
    network: str
    memory: str
    cpu_quota: int
    username: str
    state_path: str = "/state"

    @classmethod
    def from_env(cls) -> "Settings":
        """Build the policy from the deployment environment."""
        image = os.getenv("OPENCODE_IMAGE", "")
        if not re.fullmatch(IMAGE_PATTERN, image):
            message = "OPENCODE_IMAGE must be pinned to a sha256 digest"
            raise ValueError(message)
        memory = os.getenv("WORKSPACE_MEM_LIMIT", "1g")
        if not re.fullmatch(MEMORY_PATTERN, memory.lower()):
            message = "WORKSPACE_MEM_LIMIT must be a bounded 200 MiB to 4 GiB size"
            raise ValueError(message)
        cpu = int(os.getenv("WORKSPACE_CPU_QUOTA", DEFAULT_CPU_QUOTA))
        if not MIN_CPU_QUOTA <= cpu <= MAX_CPU_QUOTA:
            message = "WORKSPACE_CPU_QUOTA must be between 10000 and 400000"
            raise ValueError(message)
        token = secret_setting("RUNTIME_CONTROLLER_TOKEN")
        server_secret = secret_setting("OPENCODE_SERVER_SECRET")
        if token == server_secret:
            message = "Controller and OpenCode server credentials must differ"
            raise ValueError(message)
        return cls(
            token,
            server_secret,
            image,
            os.getenv("SKYCLOUD_DOCKER_NETWORK", "skycloud_runtime-network"),
            memory,
            cpu,
            os.getenv("OPENCODE_SERVER_USERNAME", "skycloud"),
            "/state",
        )

    def password(self, runtime_id: int, user_id: int, workspace_id: int) -> str:
        """Derive the runtime server credential from the Controller secret."""
        material = (
            f"runtime:{runtime_id}:user:{user_id}:workspace:{workspace_id}".encode()
        )
        return hmac.new(
            self.server_secret.encode(), material, hashlib.sha256,
        ).hexdigest()

    def container_options(
        self, workspace_id: int, user_id: int, runtime_id: int,
    ) -> DockerRunOptions:
        """Return the complete fixed container policy; callers add no options."""
        return {
            "image": self.image,
            "name": container_name(workspace_id, user_id),
            "detach": True,
            "command": [
                "--pure",
                "serve",
                "--hostname",
                "0.0.0.0",
                "--port",
                "3000",
            ],
            "user": RUNTIME_USER,
            "working_dir": "/workspace",
            "read_only": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "pids_limit": 128,
            "mem_limit": self.memory,
            "cpu_quota": self.cpu_quota,
            "cpu_period": 100000,
            "network": self.network,
            "restart_policy": {"Name": "no"},
            "tmpfs": {
                "/home/opencode": (
                    "rw,nosuid,nodev,size=256m,uid=10001,gid=10001,mode=700"
                ),
                "/workspace": (
                    "rw,nosuid,nodev,size=512m,uid=10001,gid=10001,mode=700"
                ),
                "/tmp": "rw,nosuid,nodev,noexec,size=64m,mode=1777",
                "/tmp/.native": (
                    "rw,exec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=700"
                ),
            },
            "environment": {
                "HOME": "/home/opencode",
                "TMPDIR": "/tmp/.native",
                "XDG_CONFIG_HOME": "/home/opencode/.config",
                "XDG_CACHE_HOME": "/home/opencode/.cache",
                "XDG_DATA_HOME": "/home/opencode/.local/share",
                "SKYCLOUD_WORKSPACE_ID": str(workspace_id),
                "SKYCLOUD_USER_ID": str(user_id),
                "SKYCLOUD_RUNTIME_ID": str(runtime_id),
                "OPENCODE_SERVER_USERNAME": self.username,
                "OPENCODE_SERVER_PASSWORD": self.password(
                    runtime_id, user_id, workspace_id,
                ),
            },
            "labels": {
                "skycloud.component": "opencode-runtime",
                "skycloud.workspace_id": str(workspace_id),
                "skycloud.user_id": str(user_id),
                "skycloud.runtime_id": str(runtime_id),
                "skycloud.runtime_policy": POLICY_VERSION,
            },
        }


def container_create_options(
    run_options: DockerRunOptions, *, name: str, generation: str,
) -> DockerCreateOptions:
    """Derive ``containers.create`` arguments from the fixed run policy."""
    labels = run_options["labels"]
    labels["skycloud.generation"] = generation
    return {
        "image": run_options["image"],
        "name": name,
        "command": run_options["command"],
        "user": run_options["user"],
        "working_dir": run_options["working_dir"],
        "read_only": run_options["read_only"],
        "cap_drop": run_options["cap_drop"],
        "security_opt": run_options["security_opt"],
        "pids_limit": run_options["pids_limit"],
        "mem_limit": run_options["mem_limit"],
        "cpu_quota": run_options["cpu_quota"],
        "cpu_period": run_options["cpu_period"],
        "network": run_options["network"],
        "restart_policy": run_options["restart_policy"],
        "tmpfs": run_options["tmpfs"],
        "environment": run_options["environment"],
        "labels": labels,
    }


def container_name(workspace_id: int, user_id: int) -> str:
    """Return the fixed name of a workspace runtime container."""
    return f"skycloud-opencode-w{workspace_id}-u{user_id}"


def generation_name(workspace_id: int, user_id: int, generation: str) -> str:
    """Return the fixed name of one runtime generation container."""
    if not re.fullmatch(GENERATION_PATTERN, generation):
        message = "Invalid runtime generation"
        raise ValueError(message)
    return container_name(workspace_id, user_id) + "-g" + generation


def matches_runtime_name(
    name: str, workspace_id: int, user_id: int, labels: dict[str, str],
) -> bool:
    """Check the container name against the identity its labels declare."""
    generation = labels.get("skycloud.generation")
    if generation is None:
        return name == container_name(workspace_id, user_id)
    return bool(re.fullmatch(GENERATION_PATTERN, generation)) and name == (
        generation_name(workspace_id, user_id, generation)
    )
