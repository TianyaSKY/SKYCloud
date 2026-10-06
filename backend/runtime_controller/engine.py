"""Docker operations restricted to managed runtimes and fixed configuration paths."""

import base64
import json
import os
import re
import shlex
import uuid
import time

import docker

from runtime_controller.generation_journal import GenerationJournal, JournalIdentityError
from runtime_controller.policy import CONFIG_PATH, POLICY_VERSION, RUNTIME_USER, container_name, generation_name, matches_runtime_name


class ControllerError(RuntimeError):
    pass


class MissingRuntime(ControllerError):
    pass


class PolicyViolation(ControllerError):
    pass


def _docker_error(name):
    candidate = getattr(getattr(docker, "errors", None), name, None)
    return candidate if isinstance(candidate, type) else MissingRuntime


class Engine:
    def __init__(self, settings, client=None):
        self.settings = settings
        host = os.getenv("DOCKER_HOST")
        self.client = client if client is not None else (docker.DockerClient(base_url=host) if host else docker.from_env())

    def ensure_image(self):
        try:
            self.client.images.get(self.settings.image)
        except _docker_error("ImageNotFound"):
            if self.settings.image.startswith("sha256:"):
                raise ControllerError("Local runtime image is missing; rebuild with scripts/build-runtime-image.py")
            self.client.images.pull(self.settings.image)

    def managed(self, ref: str, *, require_policy=False):
        if not re.fullmatch(r"(?:[0-9a-f]{12,64}|skycloud-opencode-w[1-9][0-9]*-u[1-9][0-9]*(?:-g[0-9a-f]{32})?)", ref):
            raise PolicyViolation("Invalid runtime reference")
        try:
            container = self.client.containers.get(ref)
        except _docker_error("NotFound") as exc:
            raise MissingRuntime("Runtime not found") from exc
        labels = container.attrs.get("Config", {}).get("Labels", {}) or {}
        try:
            ids = [int(labels[f"skycloud.{key}"]) for key in ("workspace_id", "user_id", "runtime_id")]
        except (KeyError, ValueError, TypeError) as exc:
            raise PolicyViolation("Container is not a managed runtime") from exc
        if min(ids) <= 0 or labels.get("skycloud.component") != "opencode-runtime" or not matches_runtime_name(container.name, ids[0], ids[1], labels):
            raise PolicyViolation("Container identity does not match runtime labels")
        if require_policy:
            host = container.attrs.get("HostConfig", {})
            networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
            if (labels.get("skycloud.runtime_policy") != POLICY_VERSION
                    or container.attrs["Config"].get("Image") != self.settings.image
                    or container.attrs["Config"].get("User") != RUNTIME_USER
                    or not host.get("ReadonlyRootfs") or set(host.get("CapDrop", [])) != {"ALL"}
                    or "no-new-privileges:true" not in host.get("SecurityOpt", [])
                    or host.get("Privileged") or host.get("Binds") or container.attrs.get("Mounts")
                    or set(networks) != {self.settings.network}):
                raise PolicyViolation("Runtime requires recreation with the current policy")
        return container

    def inspect(self, ref):
        container = self.managed(ref)
        labels = container.attrs["Config"]["Labels"]
        # Never expose environment variables or arbitrary Docker attributes.
        return {"id": container.id, "name": container.name, "status": container.status,
                "image": container.attrs["Config"].get("Image"), "labels": {key: value for key, value in labels.items() if key in {"skycloud.component", "skycloud.workspace_id", "skycloud.user_id", "skycloud.runtime_id", "skycloud.runtime_policy", "skycloud.generation"}},
                "ports": container.attrs.get("NetworkSettings", {}).get("Ports", {})}

    def user_snapshot(self, user_id):
        """One Docker list request, exposing only this user's managed identities."""
        rows = self.client.api.containers(all=True, filters={"label": [
            "skycloud.component=opencode-runtime", f"skycloud.user_id={user_id}",
        ]})
        result = []
        for row in rows:
            labels = row.get("Labels") or {}
            try:
                workspace_id = int(labels["skycloud.workspace_id"])
                owner_id = int(labels["skycloud.user_id"])
                runtime_id = int(labels["skycloud.runtime_id"])
            except (KeyError, ValueError, TypeError):
                continue
            if (min(workspace_id, owner_id, runtime_id) <= 0 or owner_id != user_id
                    or labels.get("skycloud.component") != "opencode-runtime"
                    or not any(matches_runtime_name(name.lstrip("/"),workspace_id,owner_id,labels) for name in row.get("Names", []))
                    or not re.fullmatch(r"[0-9a-f]{64}", row.get("Id", ""))):
                continue
            result.append({"id": row["Id"], "workspace_id": workspace_id,
                           "user_id": owner_id, "runtime_id": runtime_id,
                           "status": "running" if row.get("State") == "running" else "stopped"})
        return result

    def create(self, workspace_id, user_id, runtime_id):
        network = self.client.networks.get(self.settings.network)
        if not network.attrs.get("Internal"):
            raise PolicyViolation("Runtime network must be internal")
        self.ensure_image()
        name = container_name(workspace_id, user_id)
        try:
            existing = self.managed(name)
        except MissingRuntime:
            existing = None
        if existing is not None:
            existing.remove(force=True, v=True)
        container = self.client.containers.run(**self.settings.container_options(workspace_id, user_id, runtime_id))
        container.reload()
        return self.inspect(container.id)

    def _generation_record(self, workspace_id, user_id, runtime_id, generation):
        if min(workspace_id,user_id,runtime_id) <= 0 or max(workspace_id,user_id,runtime_id) > 2**63 - 1:
            raise PolicyViolation("Invalid runtime identity")
        try:
            generation_name(workspace_id,user_id,generation)
        except (ValueError,TypeError) as exc:
            raise PolicyViolation("Invalid runtime generation") from exc
        return GenerationJournal(self.settings.state_path).locked(generation,(workspace_id,user_id,runtime_id))

    def prepare(self, workspace_id, user_id, runtime_id, generation):
        try:
            with self._generation_record(workspace_id,user_id,runtime_id,generation) as record:
                state = record.read()
                if state['state'] == 'retired':
                    raise PolicyViolation("Runtime generation is retired")
                if state['container_id']:
                    container = self.managed(state['container_id'],require_policy=True)
                    labels = container.attrs['Config']['Labels']
                    if labels.get('skycloud.generation') != generation or tuple(int(labels[f'skycloud.{key}']) for key in ('workspace_id','user_id','runtime_id')) != (workspace_id,user_id,runtime_id):
                        raise PolicyViolation("Recorded generation identity does not match")
                    return self.inspect(container.id)
                result = self._prepare_container(workspace_id,user_id,runtime_id,generation)
                record.ready(result['id'])
                return result
        except JournalIdentityError as exc:
            raise PolicyViolation(str(exc)) from exc

    def retire_generation(self, workspace_id, user_id, runtime_id, generation):
        try:
            with self._generation_record(workspace_id,user_id,runtime_id,generation) as record:
                record.retire()
                state = record.read()
                ref = state['container_id'] or generation_name(workspace_id,user_id,generation)
                try:
                    container = self.managed(ref)
                except MissingRuntime:
                    return
                labels = container.attrs['Config']['Labels']
                if labels.get('skycloud.generation') != generation or tuple(int(labels[f'skycloud.{key}']) for key in ('workspace_id','user_id','runtime_id')) != (workspace_id,user_id,runtime_id):
                    raise PolicyViolation("Retired generation identity does not match")
                try:
                    container.remove(force=True,v=True)
                except _docker_error('NotFound'):
                    pass
        except JournalIdentityError as exc:
            raise PolicyViolation(str(exc)) from exc

    def _prepare_container(self, workspace_id, user_id, runtime_id, generation):
        """Idempotently create a stopped generation without replacing old resources."""
        if min(workspace_id,user_id,runtime_id) <= 0 or max(workspace_id,user_id,runtime_id) > 2**63 - 1:
            raise PolicyViolation("Invalid runtime identity")
        try:
            name = generation_name(workspace_id,user_id,generation)
        except (ValueError,TypeError) as exc:
            raise PolicyViolation("Invalid runtime generation") from exc
        network = self.client.networks.get(self.settings.network)
        if not network.attrs.get("Internal"):
            raise PolicyViolation("Runtime network must be internal")
        self.ensure_image()
        def existing():
            container = self.managed(name, require_policy=True)
            labels = container.attrs['Config']['Labels']
            if int(labels['skycloud.runtime_id']) != runtime_id:
                raise PolicyViolation("Generation belongs to another runtime")
            return self.inspect(container.id)
        try:
            return existing()
        except MissingRuntime:
            pass
        options = self.settings.container_options(workspace_id,user_id,runtime_id)
        options['name'] = name
        options['labels']['skycloud.generation'] = generation
        options.pop('detach')  # create, not run: no process before SQL admission.
        try:
            container = self.client.containers.create(**options)
        except _docker_error('APIError') as exc:
            if getattr(exc,'status_code',None) != 409:
                raise
            # Docker reserves the name before the container becomes readable.
            # Retry only transient absence, never a mismatched policy/identity.
            deadline = time.monotonic() + 2
            while True:
                try:
                    return existing()
                except MissingRuntime:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
        container.reload()
        try:
            self.managed(container.id, require_policy=True)
        except PolicyViolation:
            # An image may declare implicit volumes. Reject the new resource
            # before admission and remove only the container just created.
            container.remove(force=True, v=True)
            raise
        return self.inspect(container.id)

    def action(self, ref, action):
        container = self.managed(ref, require_policy=action != "remove")
        labels = container.attrs['Config']['Labels']
        if action == 'remove' and labels.get('skycloud.generation'):
            return self.retire_generation(*(int(labels[f'skycloud.{key}']) for key in ('workspace_id','user_id','runtime_id')),labels['skycloud.generation'])
        if action == "start":
            container.start()
        elif action == "stop":
            container.stop(timeout=10)
        elif action == "restart":
            container.restart(timeout=3)
        elif action == "remove":
            container.remove(force=True, v=True)
        else:
            raise PolicyViolation("Unsupported runtime action")

    def remove_legacy(self, ref: str):
        """Remove only an exact historical SKYCloud identity, never relay to it."""
        if not re.fullmatch(r"[0-9a-f]{64}", ref):
            raise PolicyViolation("Legacy cleanup requires a full container ID")
        try:
            container = self.client.containers.get(ref)
        except _docker_error("NotFound") as exc:
            raise MissingRuntime("Runtime not found") from exc
        labels = container.attrs.get("Config", {}).get("Labels", {}) or {}
        try:
            workspace = int(labels["skycloud.workspace_id"])
            user = int(labels["skycloud.user_id"])
        except (KeyError, ValueError, TypeError) as exc:
            raise PolicyViolation("Container is not a historical SKYCloud runtime") from exc
        if (container.id != ref or min(workspace, user) <= 0 or max(workspace,user) > 2**63 - 1
                or labels.get("skycloud.component") != "opencode-runtime"
                or labels.get("skycloud.runtime_id") not in ("", "0")
                or container.name not in (f"skycloud-workspace-{workspace}", container_name(workspace,user))):
            raise PolicyViolation("Historical container identity does not match its labels")
        try:
            container.remove(force=True, v=True)
        except _docker_error("NotFound") as exc:
            raise MissingRuntime("Runtime not found") from exc

    def read_config(self, ref):
        container = self.managed(ref, require_policy=True)
        result = container.exec_run(["cat", CONFIG_PATH], user=RUNTIME_USER)
        if result.exit_code != 0:
            return {}
        try:
            parsed = json.loads(result.output)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def write_config(self, ref, config):
        container = self.managed(ref, require_policy=True)
        # Fixed commands only; both data and the generated temp path are safely
        # quoted. Caller input never becomes a command or filesystem path.
        encoded = base64.b64encode(json.dumps(config, ensure_ascii=False).encode()).decode()
        temporary = CONFIG_PATH + ".tmp." + uuid.uuid4().hex
        command = ("umask 077; mkdir -p /home/opencode/.config/opencode && "
                   f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(temporary)} "
                   f"&& mv {shlex.quote(temporary)} {shlex.quote(CONFIG_PATH)}")
        result = container.exec_run(["sh", "-c", command], user=RUNTIME_USER)
        if result.exit_code != 0:
            raise ControllerError("Runtime configuration write failed")

    def target(self, ref):
        container = self.managed(ref, require_policy=True)
        if container.status != "running":
            raise PolicyViolation("Runtime is not running")
        labels = container.attrs["Config"]["Labels"]
        # Read actual isolated network IP instead of caller-controlled URLs.
        address = container.attrs["NetworkSettings"]["Networks"][self.settings.network]["IPAddress"]
        if not address:
            raise PolicyViolation("Runtime has no network address")
        import ipaddress
        address = str(ipaddress.IPv4Address(address))
        password = self.settings.password(int(labels["skycloud.runtime_id"]), int(labels["skycloud.user_id"]), int(labels["skycloud.workspace_id"]))
        return f"http://{address}:3000", (self.settings.username, password)
