"""Hash-only, rotating browser refresh credentials and revocable access leases."""

import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import Mapping
from typing import NotRequired, TypedDict, cast
from urllib.parse import urlsplit

import jwt
from fastapi import HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session

from app.features.auth import service as auth_service
from app.infra.extensions import SECRET_KEY, RedisArgument, RedisValue, redis_client
from app.models.user import User

ACCESS_TTL = 900
REFRESH_TTL = 7 * 24 * 60 * 60
COOKIE = "skycloud_refresh"

CREATE = """
if redis.call('EXISTS', KEYS[1]) == 1 or redis.call('EXISTS', KEYS[2]) == 1 then
  return 0
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
return 1
"""
ROTATE = """
local value = redis.call('GET', KEYS[1])
if not value then return 0 end
local family = cjson.decode(value)
if family.token_hash ~= ARGV[1] then
  redis.call('DEL', KEYS[1])
  return 0
end
if redis.call('EXISTS', KEYS[2]) == 1 then return -1 end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[4])
redis.call('SET', KEYS[2], ARGV[3], 'EX', ARGV[4])
return 1
"""


class Family(TypedDict):
    """Persisted refresh-family record validated by :func:`load_family`."""

    user_id: int
    auth_version: int
    expires_at: int
    token_hash: NotRequired[str]


class AccessClaims(TypedDict):
    """Minimal claims an access token must expose when seeding a family."""

    sub: str
    ver: int


class SessionUserPayload(TypedDict):
    """Public session user projection."""

    id: int
    username: str
    role: str | None
    avatar: str | None


class SessionPayload(TypedDict):
    """Public browser session projection returned by create/refresh."""

    token: str
    user_id: int
    role: str | None
    expires_in: int
    user: SessionUserPayload


UserRow = Row[tuple[int, str, str | None, str | None, int]]


def origin() -> str:
    """Return the configured application origin, rejecting unsafe values."""
    value = os.getenv("APP_ORIGIN", "http://app.skycloud.localhost").rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(503, "Application origin is not configured safely")
    if parsed.scheme != "https" and not (
        parsed.hostname == "localhost"
        or parsed.hostname.endswith(".localhost")
        or parsed.hostname in {"127.0.0.1", "::1"}
    ):
        raise HTTPException(503, "Browser sessions require HTTPS outside localhost")
    return value


def cookie_name() -> str:
    """Return the refresh cookie name for the current origin."""
    return ("__Host-" if origin().startswith("https://") else "") + COOKIE


def check_origin(request: Request, *, required: bool = False) -> None:
    """Reject cross-origin browser session mutations."""
    supplied = request.headers.get("origin")
    if (supplied and supplied != origin()) or (
        required
        and (
            supplied != origin()
            or request.headers.get("x-skycloud-session") != "1"
        )
    ):
        raise HTTPException(403, "Browser session origin check failed")


def credential(request: Request) -> str:
    """Read the refresh credential from the request cookies."""
    return request.cookies.get(cookie_name(), "")


def set_cookie(response: Response, raw: str, seconds: int) -> None:
    """Attach the rotating refresh credential to the response."""
    response.set_cookie(
        cookie_name(),
        raw,
        max_age=seconds,
        httponly=True,
        secure=origin().startswith("https://"),
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"


def clear_cookie(response: Response) -> None:
    """Delete the refresh credential cookie."""
    response.delete_cookie(
        cookie_name(),
        httponly=True,
        secure=origin().startswith("https://"),
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"


def digest(raw: object) -> str:
    """Validate a refresh credential shape and return its SHA-256 digest."""
    if not isinstance(raw, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", raw):
        raise HTTPException(401, "Refresh session required")
    return hashlib.sha256(raw.encode()).hexdigest()


def family_key(sid: object) -> str:
    """Validate a browser session id and return its Redis family key."""
    if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", sid):
        raise HTTPException(401, "Invalid browser session")
    return "web-auth:family:" + sid


def token_key(raw: object) -> str:
    """Return the Redis key that maps a refresh credential to its family."""
    return "web-auth:refresh:" + digest(raw)


def available(operation: str, *args: RedisArgument) -> RedisValue:
    """Run one synchronous Redis command, mapping store failures to 503."""
    try:
        return getattr(redis_client, operation)(*args)
    except Exception as exc:
        raise HTTPException(503, "Browser session store unavailable") from exc


def load_family(sid: str) -> Family:
    """Load and validate a persisted refresh family."""
    value = available("get", family_key(sid))
    try:
        return _parse_family(value)
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(401, "Browser session expired or revoked") from exc


def _parse_family(value: RedisValue) -> Family:
    """Decode and validate one persisted refresh family record."""
    raw_value: object = (
        json.loads(value)
        if isinstance(value, str | bytes | bytearray) and value
        else None
    )
    if not isinstance(raw_value, dict):
        message = "Refresh family is not a JSON object"
        raise TypeError(message)
    # Decoded JSON objects always use string keys.
    payload = cast("dict[str, object]", raw_value)
    family_user_id = payload.get("user_id")
    family_auth_version = payload.get("auth_version")
    family_expires_at = payload.get("expires_at")
    if (
        type(family_user_id) is not int
        or family_user_id <= 0
        or type(family_auth_version) is not int
        or family_auth_version < 0
        or type(family_expires_at) is not int
        or family_expires_at <= time.time()
    ):
        message = "Refresh family record is invalid"
        raise ValueError(message)
    # The runtime checks above prove the required family fields.
    return cast("Family", payload)


def family_for(raw: str) -> str:
    """Resolve a refresh credential to its validated browser session id."""
    sid = available("get", token_key(raw))
    if isinstance(sid, bytes):
        sid = sid.decode()
    family_key(sid)
    # family_key has validated both the type and shape without altering the id.
    return cast("str", sid)


def current_user(session: Session, family: Family) -> UserRow:
    """Load the live account row the family was issued for."""
    row = session.execute(
        select(User.id, User.username, User.role, User.avatar, User.auth_version)
        .where(User.id == family["user_id"]),
    ).first()
    if not row or row.auth_version != family["auth_version"]:
        raise HTTPException(401, "Browser session revoked")
    return row


def response_payload(row: UserRow, sid: str) -> SessionPayload:
    """Issue an access token bound to ``sid`` and its public projection."""
    token = auth_service.generate_token(row.id, row.auth_version, session_id=sid)
    if not token:
        raise HTTPException(503, "Unable to issue access session")
    return {
        "token": token,
        "user_id": row.id,
        "role": row.role,
        "expires_in": ACCESS_TTL,
        "user": {
            "id": row.id,
            "username": row.username,
            "role": row.role,
            "avatar": row.avatar,
        },
    }


def create(
    session: Session, access_claims: AccessClaims,
) -> tuple[str, SessionPayload]:
    """Create a browser refresh family and its first access session."""
    sid, raw = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    family: Family = {
        "user_id": int(access_claims["sub"]),
        "auth_version": access_claims["ver"],
        "expires_at": int(time.time()) + REFRESH_TTL,
        "token_hash": digest(raw),
    }
    row = current_user(session, family)
    result = response_payload(row, sid)
    created = available(
        "eval",
        CREATE,
        2,
        family_key(sid),
        token_key(raw),
        json.dumps(family),
        sid,
        REFRESH_TTL,
    )
    if created != 1:
        raise HTTPException(503, "Unable to create browser session")
    return raw, result


def refresh(session: Session, raw: str) -> tuple[str, SessionPayload, int]:
    """Rotate a refresh credential, rejecting reuse of a consumed family."""
    sid = family_for(raw)
    family = load_family(sid)
    row = current_user(session, family)
    replacement = secrets.token_urlsafe(32)
    lifetime = int(family["expires_at"] - time.time())
    if lifetime <= 0:
        raise HTTPException(401, "Browser session expired")
    result = response_payload(row, sid)
    updated = {**family, "token_hash": digest(replacement)}
    rotated = available(
        "eval",
        ROTATE,
        2,
        family_key(sid),
        token_key(replacement),
        digest(raw),
        json.dumps(updated),
        sid,
        lifetime,
    )
    if rotated == -1:
        raise HTTPException(503, "Unable to rotate browser session")
    if rotated != 1:
        raise HTTPException(
            401, "Refresh credential already used; browser session revoked",
        )
    return replacement, result, lifetime


def revoke(raw: str) -> None:
    """Revoke the family behind a refresh credential when it is still valid."""
    if not raw:
        return
    try:
        sid = family_for(raw)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            return
        raise
    available("delete", family_key(sid))


def revoke_access(authorization: str) -> None:
    """Revoke the family bound to a still-valid access token."""
    try:
        scheme, token = authorization.split(" ", 1)
        if scheme.lower() != "bearer":
            return
        claims = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        if claims.get("type") is None and "sid" in claims:
            available("delete", family_key(claims["sid"]))
    except (ValueError, jwt.InvalidTokenError):
        return


def access_active(payload: Mapping[str, object]) -> bool:
    """Return whether a browser access token still has a live family."""
    try:
        sid = payload["sid"]
        if not isinstance(sid, str):
            return False
        family = load_family(sid)
        return (
            family["user_id"] == int(cast("str | int", payload["sub"]))
            and family["auth_version"] == payload["ver"]
        )
    except HTTPException as exc:
        if exc.status_code != status.HTTP_401_UNAUTHORIZED:
            raise
        return False
    except (KeyError, TypeError, ValueError):
        return False
