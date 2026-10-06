"""Fixed container policy; callers cannot supply Docker flags, mounts or commands."""

from dataclasses import dataclass
import hashlib
import hmac
import os
import re

CONFIG_PATH = "/home/opencode/.config/opencode/opencode.json"
RUNTIME_USER = "10001:10001"
POLICY_VERSION = "4"


def secret_setting(name: str) -> str:
    value = os.getenv(name, "")
    if len(value) < 32 or "replace-with" in value.lower() or value in {"sky_cloud_secret_key_dev_only"}:
        raise ValueError(f"{name} must contain a distinct random secret of at least 32 characters")
    return value


@dataclass(frozen=True)
class Settings:
    token: str
    server_secret: str
    image: str
    network: str
    memory: str
    cpu_quota: int
    username: str

    @classmethod
    def from_env(cls):
        image = os.getenv("OPENCODE_IMAGE", "")
        if not re.fullmatch(r"(?:[a-zA-Z0-9./:_-]+@)?sha256:[0-9a-f]{64}", image):
            raise ValueError("OPENCODE_IMAGE must be pinned to a sha256 digest")
        memory = os.getenv("WORKSPACE_MEM_LIMIT", "1g")
        if not re.fullmatch(r"(?:[1-4]g|(?:[2-9][0-9]{2}|[1-3][0-9]{3}|40[0-8][0-9]|409[0-6])m)", memory.lower()):
            raise ValueError("WORKSPACE_MEM_LIMIT must be a bounded 200 MiB to 4 GiB size")
        cpu = int(os.getenv("WORKSPACE_CPU_QUOTA", "100000"))
        if not 10000 <= cpu <= 400000:
            raise ValueError("WORKSPACE_CPU_QUOTA must be between 10000 and 400000")
        token = secret_setting("RUNTIME_CONTROLLER_TOKEN")
        server_secret = secret_setting("OPENCODE_SERVER_SECRET")
        if token == server_secret:
            raise ValueError("Controller and OpenCode server credentials must differ")
        return cls(
            token, server_secret,
            image, os.getenv("SKYCLOUD_DOCKER_NETWORK", "skycloud_runtime-network"),
            memory, cpu, os.getenv("OPENCODE_SERVER_USERNAME", "skycloud"),
        )

    def password(self, runtime_id: int, user_id: int, workspace_id: int) -> str:
        material = f"runtime:{runtime_id}:user:{user_id}:workspace:{workspace_id}".encode()
        return hmac.new(self.server_secret.encode(), material, hashlib.sha256).hexdigest()

    def container_options(self, workspace_id: int, user_id: int, runtime_id: int) -> dict:
        return {
            "image": self.image, "name": container_name(workspace_id, user_id), "detach": True,
            "command": ["--pure", "serve", "--hostname", "0.0.0.0", "--port", "3000"],
            "user": RUNTIME_USER, "working_dir": "/workspace", "read_only": True,
            "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"], "pids_limit": 128,
            "mem_limit": self.memory, "cpu_quota": self.cpu_quota, "cpu_period": 100000,
            "network": self.network, "restart_policy": {"Name": "no"},
            "tmpfs": {
                "/home/opencode": "rw,nosuid,nodev,size=256m,uid=10001,gid=10001,mode=700",
                "/workspace": "rw,nosuid,nodev,size=512m,uid=10001,gid=10001,mode=700",
                "/tmp": "rw,nosuid,nodev,noexec,size=64m,mode=1777",
                "/tmp/.native": "rw,exec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=700",
            },
            "environment": {
                "HOME": "/home/opencode", "TMPDIR": "/tmp/.native", "XDG_CONFIG_HOME": "/home/opencode/.config",
                "XDG_CACHE_HOME": "/home/opencode/.cache", "XDG_DATA_HOME": "/home/opencode/.local/share",
                "SKYCLOUD_WORKSPACE_ID": str(workspace_id), "SKYCLOUD_USER_ID": str(user_id),
                "SKYCLOUD_RUNTIME_ID": str(runtime_id), "OPENCODE_SERVER_USERNAME": self.username,
                "OPENCODE_SERVER_PASSWORD": self.password(runtime_id, user_id, workspace_id),
            },
            "labels": {
                "skycloud.component": "opencode-runtime", "skycloud.workspace_id": str(workspace_id),
                "skycloud.user_id": str(user_id), "skycloud.runtime_id": str(runtime_id),
                "skycloud.runtime_policy": POLICY_VERSION,
            },
        }


def container_name(workspace_id: int, user_id: int) -> str:
    return f"skycloud-opencode-w{workspace_id}-u{user_id}"


def generation_name(workspace_id: int, user_id: int, generation: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", generation):
        raise ValueError("Invalid runtime generation")
    return container_name(workspace_id,user_id) + "-g" + generation


def matches_runtime_name(name: str, workspace_id: int, user_id: int, labels: dict) -> bool:
    generation = labels.get("skycloud.generation")
    if generation is None:
        return name == container_name(workspace_id,user_id)
    return isinstance(generation,str) and bool(re.fullmatch(r"[0-9a-f]{32}", generation)) and name == generation_name(workspace_id,user_id,generation)
