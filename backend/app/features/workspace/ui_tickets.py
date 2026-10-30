"""Opaque, one-use UI tickets and host-bound, revocable runtime sessions."""

import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import TypedDict
from urllib.parse import SplitResult, urlsplit

from fastapi import HTTPException
from runtime_controller.json_types import JsonObject, JsonValue
from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.features.auth.refresh_sessions import access_active
from app.infra.extensions import redis_client
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.user import User
from app.models.workspace import WorkspaceMember

TICKET_TTL = 30
SESSION_TTL = 600
COOKIE = "skycloud_runtime_ui"
DEFAULT_ORIGIN_TEMPLATE = "http://r{runtime_id}.skycloud.localhost:8081"
DEFAULT_APP_ORIGIN = "http://app.skycloud.localhost"
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
EDITOR_ROLES = frozenset({"admin", "editor"})

NOT_CONFIGURED = "Runtime UI origin is not configured safely"
DISTINCT_HOST_REQUIRED = "Runtime UI requires a distinct host for each runtime"
CREDENTIAL_REQUIRED = "Runtime UI credential required"
STORE_UNAVAILABLE = "Runtime UI credential store unavailable"
CREDENTIAL_EXPIRED = "Runtime UI credential expired or consumed"
INVALID_CREDENTIAL = "Invalid runtime UI credential"
HOST_MISMATCH = "Runtime UI host mismatch"
SESSION_REVOKED = "Runtime UI session revoked"
PARENT_REVOKED = "Parent login session revoked"
EDITOR_REQUIRED = "Runtime UI requires current editor membership"
NOT_RUNNING = "Runtime is not running"


class UiTicket(TypedDict):
    """The launch credential returned to the browser."""

    launch_url: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class _Origin:
    scheme: str
    hostname: str
    port: int | None


@dataclass(frozen=True, slots=True)
class _Claims:
    user_id: int
    workspace_id: int
    runtime_id: int
    auth_version: JsonValue
    container_id: JsonValue
    origin: JsonValue


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    """Authoritative runtime identity used by one UI credential check."""

    runtime_id: int
    container_id: str
    status: str
    auth_version: int
    role: str


@dataclass(frozen=True, slots=True)
class CredentialFields:
    """Fields of an already validated session credential."""

    user_id: int
    container_id: str
    origin: str
    expires_at: float


def _origin_fields(value: str) -> _Origin:
    url: SplitResult = urlsplit(value)
    hostname = url.hostname
    if (
        url.scheme not in {"http", "https"}
        or not hostname
        or url.username
        or url.password
        or url.path not in {"", "/"}
        or url.query
        or url.fragment
    ):
        raise ValueError
    return _Origin(scheme=url.scheme, hostname=hostname, port=url.port)


def _parse_origin(value: str) -> _Origin:
    try:
        parsed = _origin_fields(value)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=NOT_CONFIGURED) from exc
    return parsed


def _canonical(origin: _Origin) -> str:
    default_port = 80 if origin.scheme == "http" else 443
    port = "" if origin.port in {None, default_port} else ":" + str(origin.port)
    return origin.scheme + "://" + origin.hostname.lower() + port


def ui_origin(runtime_id: int) -> str:
    """Return the canonical per-runtime UI origin or reject unsafe config."""
    template = os.getenv("RUNTIME_UI_ORIGIN_TEMPLATE", DEFAULT_ORIGIN_TEMPLATE)
    parent = os.getenv("APP_ORIGIN", DEFAULT_APP_ORIGIN)
    if template.count("{runtime_id}") != 1 or runtime_id <= 0:
        raise HTTPException(status_code=503, detail=NOT_CONFIGURED)
    value = template.replace("{runtime_id}", str(runtime_id))
    target = _parse_origin(value)
    other = _parse_origin(template.replace("{runtime_id}", str(runtime_id + 1)))
    app = _parse_origin(parent)
    if (
        target.hostname in {other.hostname, app.hostname}
        or _canonical(target) == _canonical(app)
    ):
        raise HTTPException(status_code=503, detail=DISTINCT_HOST_REQUIRED)
    return _canonical(target)


def cookie_name(origin: str) -> str:
    """Return the cookie name that is safe for this origin's scheme."""
    return "__Host-" + COOKIE if origin.startswith("https://") else COOKIE


def _key(kind: str, raw: object) -> str:
    if not isinstance(raw, str) or not TOKEN_PATTERN.fullmatch(raw):
        raise HTTPException(status_code=401, detail=CREDENTIAL_REQUIRED)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return "runtime-ui:" + kind + ":" + digest


def _store(kind: str, payload: JsonObject, ttl: int) -> str:
    raw = secrets.token_urlsafe(32)
    stored = payload
    if kind == "session":
        stored = {**payload, "expires_at": time.time() + ttl}
    try:
        accepted = redis_client.set(
            _key(kind, raw), json.dumps(stored), ex=ttl, nx=True,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=STORE_UNAVAILABLE) from exc
    if not accepted:
        raise HTTPException(status_code=503, detail=STORE_UNAVAILABLE)
    return raw


def _load(kind: str, raw: str, *, consume: bool = False) -> JsonObject:
    key = _key(kind, raw)
    try:
        value = redis_client.getdel(key) if consume else redis_client.get(key)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=STORE_UNAVAILABLE) from exc
    if not value:
        raise HTTPException(status_code=401, detail=CREDENTIAL_EXPIRED)
    try:
        payload = _decode_payload(value, kind)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=401, detail=INVALID_CREDENTIAL) from exc
    return payload


def _decode_payload(value: str, kind: str) -> JsonObject:
    payload: JsonValue = json.loads(value)
    if not isinstance(payload, dict):
        raise TypeError
    if kind == "session":
        expires_at = payload.get("expires_at")
        if not isinstance(expires_at, (int, float)) or expires_at <= time.time():
            raise ValueError
    return payload


def _identity(
    session: Session,
    user_id: int,
    workspace_id: int,
    runtime_id: int | None = None,
) -> RuntimeIdentity:
    # Scalar authoritative reads avoid stale ORM/profile caches after revocation.
    query = session.query(
        OpenCodeRuntime.id,
        OpenCodeRuntime.container_id,
        OpenCodeRuntime.status,
        User.auth_version,
        WorkspaceMember.role,
    )
    query = query.join(User, User.id == OpenCodeRuntime.user_id).join(
        WorkspaceMember,
        and_(
            WorkspaceMember.workspace_id == OpenCodeRuntime.workspace_id,
            WorkspaceMember.user_id == OpenCodeRuntime.user_id,
        ),
    )
    query = query.filter(
        OpenCodeRuntime.user_id == user_id,
        OpenCodeRuntime.workspace_id == workspace_id,
    )
    if runtime_id is not None:
        query = query.filter(OpenCodeRuntime.id == runtime_id)
    row = query.first()
    if row is None or row.role not in EDITOR_ROLES:
        raise HTTPException(status_code=403, detail=EDITOR_REQUIRED)
    container_id = row.container_id
    if row.status != "running" or not container_id:
        raise HTTPException(status_code=409, detail=NOT_RUNNING)
    return RuntimeIdentity(
        runtime_id=int(row.id),
        container_id=container_id,
        status=row.status,
        auth_version=int(row.auth_version),
        role=row.role,
    )


def _int_field(payload: JsonObject, name: str) -> int:
    value = payload[name]
    if not isinstance(value, (str, int, float)):
        raise TypeError
    return int(value)


def _claims(payload: JsonObject) -> _Claims:
    try:
        return _Claims(
            user_id=_int_field(payload, "user_id"),
            workspace_id=_int_field(payload, "workspace_id"),
            runtime_id=_int_field(payload, "runtime_id"),
            auth_version=payload["auth_version"],
            container_id=payload["container_id"],
            origin=payload["origin"],
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=401, detail=INVALID_CREDENTIAL) from exc


def issue(
    session: Session,
    user_id: int,
    workspace_id: int,
    *,
    parent_session: str | None = None,
) -> UiTicket:
    """Issue one opaque launch ticket for the caller's running runtime."""
    identity = _identity(session, user_id, workspace_id)
    origin = ui_origin(identity.runtime_id)
    payload: JsonObject = {
        "user_id": user_id,
        "workspace_id": workspace_id,
        "runtime_id": identity.runtime_id,
        "container_id": identity.container_id,
        "auth_version": identity.auth_version,
        "origin": origin,
    }
    if parent_session is not None:
        payload["parent_session"] = parent_session
    raw = _store("ticket", payload, TICKET_TTL)
    return UiTicket(
        launch_url=origin + "/__skycloud/launch?ticket=" + raw,
        expires_in=TICKET_TTL,
    )


def validate(session: Session, payload: JsonObject, host: str) -> JsonObject:
    """Prove a credential still matches current runtime identity and host."""
    claims = _claims(payload)
    origin = ui_origin(claims.runtime_id)
    if (
        claims.origin != origin
        or urlsplit(origin).netloc.lower() != host.lower()
    ):
        raise HTTPException(status_code=403, detail=HOST_MISMATCH)
    identity = _identity(
        session, claims.user_id, claims.workspace_id, claims.runtime_id,
    )
    if (
        identity.auth_version != claims.auth_version
        or identity.container_id != claims.container_id
    ):
        raise HTTPException(status_code=401, detail=SESSION_REVOKED)
    if "parent_session" in payload and not access_active(
        {
            "sid": payload["parent_session"],
            "sub": str(claims.user_id),
            "ver": identity.auth_version,
        },
    ):
        raise HTTPException(status_code=401, detail=PARENT_REVOKED)
    return payload


def redeem(session: Session, raw: str, host: str) -> tuple[str, JsonObject]:
    """Consume one launch ticket and issue its host-bound session."""
    payload = validate(session, _load("ticket", raw, consume=True), host)
    return _store("session", payload, SESSION_TTL), payload


def authenticate(session: Session, raw: str, host: str) -> JsonObject:
    """Validate a host-bound session credential."""
    return validate(session, _load("session", raw), host)


def credential_origin(payload: JsonObject) -> str:
    """Return the origin of an already validated UI credential."""
    return _credential_str(payload, "origin")


def credential_fields(payload: JsonObject) -> CredentialFields:
    """Project an already validated session credential for the gateway."""
    expires_at = payload.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        raise HTTPException(status_code=401, detail=INVALID_CREDENTIAL)
    return CredentialFields(
        user_id=_credential_int(payload, "user_id"),
        container_id=_credential_str(payload, "container_id"),
        origin=_credential_str(payload, "origin"),
        expires_at=float(expires_at),
    )


def _credential_str(payload: JsonObject, name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise HTTPException(status_code=401, detail=INVALID_CREDENTIAL)
    return value


def _credential_int(payload: JsonObject, name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int):
        raise HTTPException(status_code=401, detail=INVALID_CREDENTIAL)
    return value
