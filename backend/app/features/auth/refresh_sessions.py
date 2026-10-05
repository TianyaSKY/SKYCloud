"""Hash-only, rotating browser refresh credentials and revocable access leases."""
import hashlib
import json
import os
import re
import secrets
import time
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import select

from app.infra.extensions import redis_client
from app.models.user import User

ACCESS_TTL = 900
REFRESH_TTL = 7 * 24 * 60 * 60
COOKIE = 'skycloud_refresh'

CREATE = '''
if redis.call('EXISTS', KEYS[1]) == 1 or redis.call('EXISTS', KEYS[2]) == 1 then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[3])
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
return 1
'''
ROTATE = '''
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
'''


def origin():
    value = os.getenv('APP_ORIGIN', 'http://app.skycloud.localhost').rstrip('/')
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment):
        raise HTTPException(503, 'Application origin is not configured safely')
    if parsed.scheme != 'https' and not (parsed.hostname == 'localhost' or parsed.hostname.endswith('.localhost')
                                       or parsed.hostname in {'127.0.0.1', '::1'}):
        raise HTTPException(503, 'Browser sessions require HTTPS outside localhost')
    return value


def cookie_name():
    return ('__Host-' if origin().startswith('https://') else '') + COOKIE


def check_origin(request, *, required=False):
    supplied = request.headers.get('origin')
    if (supplied and supplied != origin()) or (required and (
            supplied != origin() or request.headers.get('x-skycloud-session') != '1')):
        raise HTTPException(403, 'Browser session origin check failed')


def credential(request):
    return request.cookies.get(cookie_name(), '')


def set_cookie(response, raw, seconds):
    response.set_cookie(cookie_name(), raw, max_age=seconds, httponly=True,
                        secure=origin().startswith('https://'), samesite='strict', path='/')
    response.headers['Cache-Control'] = 'no-store'


def clear_cookie(response):
    response.delete_cookie(cookie_name(), httponly=True, secure=origin().startswith('https://'),
                           samesite='strict', path='/')
    response.headers['Cache-Control'] = 'no-store'


def digest(raw):
    if not isinstance(raw, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', raw):
        raise HTTPException(401, 'Refresh session required')
    return hashlib.sha256(raw.encode()).hexdigest()


def family_key(sid):
    if not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', sid):
        raise HTTPException(401, 'Invalid browser session')
    return 'web-auth:family:' + sid


def token_key(raw):
    return 'web-auth:refresh:' + digest(raw)


def available(operation, *args):
    try:
        return getattr(redis_client, operation)(*args)
    except Exception as exc:
        raise HTTPException(503, 'Browser session store unavailable') from exc


def load_family(sid):
    value = available('get', family_key(sid))
    try:
        payload = json.loads(value) if value else None
        if (not isinstance(payload, dict) or type(payload.get('user_id')) is not int or payload['user_id'] <= 0
                or type(payload.get('auth_version')) is not int or payload['auth_version'] < 0
                or type(payload.get('expires_at')) is not int or payload['expires_at'] <= time.time()):
            raise ValueError()
        return payload
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(401, 'Browser session expired or revoked') from exc


def family_for(raw):
    sid = available('get', token_key(raw))
    if isinstance(sid, bytes):
        sid = sid.decode()
    family_key(sid)
    return sid


def current_user(session, family):
    row = session.execute(select(User.id, User.username, User.role, User.avatar, User.auth_version)
                          .where(User.id == family['user_id'])).first()
    if not row or row.auth_version != family['auth_version']:
        raise HTTPException(401, 'Browser session revoked')
    return row


def response_payload(row, sid):
    from app.features.auth.service import generate_token
    token = generate_token(row.id, row.auth_version, session_id=sid)
    if not token:
        raise HTTPException(503, 'Unable to issue access session')
    return dict(token=token, user_id=row.id, role=row.role, expires_in=ACCESS_TTL,
                user=dict(id=row.id, username=row.username, role=row.role, avatar=row.avatar))


def create(session, access_claims):
    sid, raw = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    family = dict(user_id=int(access_claims['sub']), auth_version=access_claims['ver'],
                  expires_at=int(time.time()) + REFRESH_TTL, token_hash=digest(raw))
    row = current_user(session, family)
    result = response_payload(row, sid)
    if available('eval', CREATE, 2, family_key(sid), token_key(raw), json.dumps(family), sid, REFRESH_TTL) != 1:
        raise HTTPException(503, 'Unable to create browser session')
    return raw, result


def refresh(session, raw):
    sid = family_for(raw)
    family = load_family(sid)
    row = current_user(session, family)
    replacement = secrets.token_urlsafe(32)
    lifetime = int(family['expires_at'] - time.time())
    if lifetime <= 0:
        raise HTTPException(401, 'Browser session expired')
    result = response_payload(row, sid)
    updated = {**family, 'token_hash': digest(replacement)}
    rotated = available('eval', ROTATE, 2, family_key(sid), token_key(replacement), digest(raw),
                        json.dumps(updated), sid, lifetime)
    if rotated == -1:
        raise HTTPException(503, 'Unable to rotate browser session')
    if rotated != 1:
        raise HTTPException(401, 'Refresh credential already used; browser session revoked')
    return replacement, result, lifetime


def revoke(raw):
    if not raw:
        return
    try:
        sid = family_for(raw)
    except HTTPException as exc:
        if exc.status_code == 401:
            return
        raise
    available('delete', family_key(sid))


def revoke_access(authorization):
    import jwt
    from app.infra.extensions import SECRET_KEY
    try:
        scheme, token = authorization.split(' ', 1)
        if scheme.lower() != 'bearer':
            return
        claims = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
        if claims.get('type') is None and 'sid' in claims:
            available('delete', family_key(claims['sid']))
    except (ValueError, jwt.InvalidTokenError):
        return


def access_active(payload):
    try:
        family = load_family(payload['sid'])
        return family['user_id'] == int(payload['sub']) and family['auth_version'] == payload['ver']
    except (HTTPException, KeyError, TypeError, ValueError):
        return False
