"""Opaque, one-use UI tickets and host-bound, revocable runtime sessions."""
import hashlib
import json
import os
import re
import secrets
import time
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.infra.extensions import redis_client
from app.models.user import User
from app.models.workspace import WorkspaceMember
from app.models.opencode_runtime import OpenCodeRuntime

TICKET_TTL = 30
SESSION_TTL = 600
COOKIE = 'skycloud_runtime_ui'


def ui_origin(runtime_id: int) -> str:
    template = os.getenv('RUNTIME_UI_ORIGIN_TEMPLATE', 'http://r{runtime_id}.skycloud.localhost:8081')
    parent = os.getenv('APP_ORIGIN', 'http://app.skycloud.localhost')
    def parse(value):
        try:
            url = urlsplit(value)
            if (url.scheme not in {'http', 'https'} or not url.hostname or url.username
                    or url.password or url.path not in {'', '/'} or url.query or url.fragment):
                raise ValueError()
            _ = url.port
            return url
        except ValueError as exc:
            raise HTTPException(503, 'Runtime UI origin is not configured safely') from exc
    if template.count('{runtime_id}') != 1 or runtime_id <= 0:
        raise HTTPException(503, 'Runtime UI origin is not configured safely')
    value = template.replace('{runtime_id}', str(runtime_id))
    target, other, app = parse(value), parse(template.replace('{runtime_id}', str(runtime_id + 1))), parse(parent)
    def canonical(url):
        port = '' if url.port in {None, 80 if url.scheme == 'http' else 443} else ':' + str(url.port)
        return url.scheme + '://' + url.hostname.lower() + port
    if target.hostname == other.hostname or canonical(target) == canonical(app):
        raise HTTPException(503, 'Runtime UI requires a distinct host for each runtime')
    return canonical(target)


def cookie_name(origin):
    return '__Host-' + COOKIE if origin.startswith('https://') else COOKIE


def _key(kind, raw):
    if not isinstance(raw, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', raw):
        raise HTTPException(401, 'Runtime UI credential required')
    return 'runtime-ui:' + kind + ':' + hashlib.sha256(raw.encode()).hexdigest()


def _store(kind, payload, ttl):
    raw = secrets.token_urlsafe(32)
    if kind == 'session':
        payload = {**payload, 'expires_at': time.time() + ttl}
    try:
        if not redis_client.set(_key(kind, raw), json.dumps(payload), ex=ttl, nx=True):
            raise HTTPException(503, 'Runtime UI credential store unavailable')
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(503, 'Runtime UI credential store unavailable') from exc
    return raw


def _load(kind, raw, *, consume=False):
    key = _key(kind, raw)
    try:
        value = redis_client.getdel(key) if consume else redis_client.get(key)
    except Exception as exc:
        raise HTTPException(503, 'Runtime UI credential store unavailable') from exc
    if not value:
        raise HTTPException(401, 'Runtime UI credential expired or consumed')
    try:
        payload = json.loads(value)
        if not isinstance(payload, dict):
            raise ValueError()
        if kind == 'session' and (not isinstance(payload.get('expires_at'), (int, float))
                                  or payload['expires_at'] <= time.time()):
            raise ValueError()
        return payload
    except (ValueError, TypeError) as exc:
        raise HTTPException(401, 'Invalid runtime UI credential') from exc


def _identity(session: Session, user_id: int, workspace_id: int, runtime_id: int | None = None):
    # Scalar authoritative reads avoid stale ORM/profile caches after revocation.
    query = session.query(OpenCodeRuntime.id, OpenCodeRuntime.container_id,
                          OpenCodeRuntime.status, User.auth_version, WorkspaceMember.role)
    query = query.join(User, User.id == OpenCodeRuntime.user_id).join(WorkspaceMember, and_(
        WorkspaceMember.workspace_id == OpenCodeRuntime.workspace_id,
        WorkspaceMember.user_id == OpenCodeRuntime.user_id))
    query = query.filter(OpenCodeRuntime.user_id == user_id, OpenCodeRuntime.workspace_id == workspace_id)
    if runtime_id is not None:
        query = query.filter(OpenCodeRuntime.id == runtime_id)
    row = query.first()
    if not row or row.role not in {'admin', 'editor'}:
        raise HTTPException(403, 'Runtime UI requires current editor membership')
    if row.status != 'running' or not row.container_id:
        raise HTTPException(409, 'Runtime is not running')
    return row


def issue(session: Session, user_id: int, workspace_id: int):
    row = _identity(session, user_id, workspace_id)
    origin = ui_origin(row.id)
    payload = dict(user_id=user_id, workspace_id=workspace_id, runtime_id=row.id,
                   container_id=row.container_id, auth_version=row.auth_version, origin=origin)
    raw = _store('ticket', payload, TICKET_TTL)
    return {'launch_url': origin + '/__skycloud/launch?ticket=' + raw, 'expires_in': TICKET_TTL}


def validate(session: Session, payload: dict, host: str):
    try:
        origin = ui_origin(int(payload['runtime_id']))
        if payload['origin'] != origin or urlsplit(origin).netloc.lower() != host.lower():
            raise HTTPException(403, 'Runtime UI host mismatch')
        row = _identity(session, int(payload['user_id']), int(payload['workspace_id']), int(payload['runtime_id']))
        if row.auth_version != payload['auth_version'] or row.container_id != payload['container_id']:
            raise HTTPException(401, 'Runtime UI session revoked')
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(401, 'Invalid runtime UI credential') from exc
    return payload


def redeem(session: Session, raw: str, host: str):
    payload = validate(session, _load('ticket', raw, consume=True), host)
    return _store('session', payload, SESSION_TTL), payload


def authenticate(session: Session, raw: str, host: str):
    return validate(session, _load('session', raw), host)
