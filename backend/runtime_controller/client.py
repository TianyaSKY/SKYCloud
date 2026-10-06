"""Application-side RPC client. It has no Docker SDK or socket access."""

import os
import re
from urllib.parse import quote, urlsplit

import httpx



class ControllerError(RuntimeError):
    pass


class ControllerNotFound(ControllerError):
    pass


class ControllerGenerationMissing(ControllerError):
    def __init__(self, generation, container_id):
        self.generation, self.container_id = generation, container_id
        super().__init__('Recorded runtime candidate is missing')


def controller_url():
    return os.getenv("RUNTIME_CONTROLLER_URL", "http://127.0.0.1:5003").rstrip("/")


def auth_headers(base_url=None):
    if base_url is not None:
        candidate, expected = urlsplit(base_url), urlsplit(controller_url())
        if (candidate.scheme, candidate.netloc) != (expected.scheme, expected.netloc) or not candidate.path.startswith(expected.path.rstrip("/") + "/runtimes/"):
            return {}
    token = os.getenv("RUNTIME_CONTROLLER_TOKEN", "")
    if not token:
        raise ControllerError("Runtime controller credential is not configured")
    return {"X-Runtime-Controller-Token": token}


def runtime_url(ref):
    return controller_url() + "/runtimes/" + quote(str(ref), safe="") + "/http"


class ControllerClient:
    def __init__(self):
        self.containers = self
        self.images = _Images(self)

    def call(self, method, path, **kwargs):
        try:
            response = httpx.request(method, controller_url() + path, headers=auth_headers(),
                                     timeout=180, trust_env=False, follow_redirects=False, **kwargs)
        except httpx.HTTPError as exc:
            raise ControllerError("Runtime controller is unavailable") from exc
        if response.status_code == 404:
            raise ControllerNotFound("Runtime not found")
        if response.status_code == 409 and path == '/containers/prepare':
            try:
                detail = response.json()
                if (detail.get('code') == 'generation_candidate_missing' and
                    detail.get('generation') == kwargs.get('json',{}).get('generation') and
                    re.fullmatch(r'[0-9a-f]{32}',detail.get('generation','')) and
                    re.fullmatch(r'[0-9a-f]{64}',detail.get('container_id',''))):
                    raise ControllerGenerationMissing(detail['generation'],detail['container_id'])
            except (ValueError,AttributeError,TypeError):
                pass
        if response.status_code >= 400:
            raise ControllerError("Runtime controller rejected operation")
        return response.json()

    def get(self, ref):
        return RemoteContainer(self, self.call("GET", "/containers/" + quote(str(ref), safe="")))

    def prepare(self, workspace_id, user_id, runtime_id, generation):
        try:
            data = self.call("POST", "/containers/prepare", json={"workspace_id":int(workspace_id), "user_id":int(user_id), "runtime_id":int(runtime_id), "generation":generation})
        except ControllerNotFound as exc:
            raise ControllerError("Runtime generation preparation could not be confirmed") from exc
        return RemoteContainer(self,data)

    def retire_generation(self, workspace_id, user_id, runtime_id, generation):
        try:
            return self.call("POST", "/containers/retire-generation", json={"workspace_id":int(workspace_id), "user_id":int(user_id), "runtime_id":int(runtime_id), "generation":generation})
        except ControllerNotFound as exc:
            raise ControllerError("Runtime generation retirement could not be confirmed") from exc

    def remove_legacy(self, ref):
        try:
            return self.call("POST", "/retired-containers/" + quote(str(ref), safe="") + "/remove")
        except ControllerNotFound as exc:
            # Old Controllers return 404 for an unsupported route as well as
            # absent containers. Only the established inspect route can prove
            # absence; retry there instead of discarding an unconfirmed intent.
            raise ControllerError("Legacy cleanup outcome could not be confirmed") from exc

    def user_snapshot(self, user_id):
        return self.call("GET", f"/users/{int(user_id)}/containers")

    def create(self, workspace_id, user_id, runtime_id):
        data = self.call("POST", "/containers/create", json={"workspace_id": int(workspace_id), "user_id": int(user_id), "runtime_id": int(runtime_id)})
        return RemoteContainer(self, data)


class _Images:
    def __init__(self, client):
        self.client = client

    def get(self, ref):
        result = self.client.call("POST", "/image/ensure")
        if result["image"] != ref:
            raise ControllerError("Runtime image differs from controller policy")
        return result

    pull = get


class RemoteContainer:
    def __init__(self, client, data):
        self.client, self.id = client, data["id"]
        self.short_id, self.name, self.status = self.id[:12], data["name"], data["status"]
        self.attrs = {"Config": {"Image": data["image"], "Labels": data["labels"]},
                      "NetworkSettings": {"Ports": data["ports"]}}

    def _path(self, suffix=""):
        return "/containers/" + self.id + suffix

    def _action(self, action):
        return self.client.call("POST", self._path("/actions/" + action))

    def start(self):
        self._action("start")

    def stop(self, **kwargs):
        self._action("stop")

    def restart(self, **kwargs):
        self._action("restart")

    def remove(self, **kwargs):
        self._action("remove")

    def read_config(self):
        return self.client.call("GET", self._path("/config"))

    def write_config(self, config):
        self.client.call("PUT", self._path("/config"), json=config)
