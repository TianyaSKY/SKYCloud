import json
from functools import wraps

import jwt
from flask import request, jsonify, current_app

from app.extensions import redis_client
from app.models.user import User


def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        if 'Authorization' in request.headers:
            auth_header = request.headers['Authorization']
            parts = auth_header.split()
            if len(parts) == 2 and parts[0] == 'Bearer':
                token = parts[1]

        if not token:
            return jsonify({'message': 'Token is missing!'}), 401
        try:
            data = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=['HS256'])
            cache_key = f"user:profile:{data['sub']}"
            cached_user_json = redis_client.get(cache_key)
            if cached_user_json:
                current_user = User.from_cache(json.loads(cached_user_json))
            else:
                current_user = User.query.get(data['sub'])
                if current_user:
                    redis_client.set(
                        cache_key,
                        json.dumps(current_user.to_dict()),
                        ex=3600
                    )
            if not current_user:
                return jsonify({'message': 'User not found!'}), 401
        except jwt.ExpiredSignatureError:
            return jsonify({'message': 'Token expired!'}), 401
        except jwt.InvalidTokenError as e:
            return jsonify({'message': 'Invalid token!', 'details': str(e)}), 401
        except Exception as e:
            return jsonify({'message': 'Token is invalid!', 'error': str(e)}), 401

        return f(current_user, *args, **kwargs)

    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(current_user, *args, **kwargs):
        if current_user.role != 'admin':
            return jsonify({'message': 'Admin privilege required'}), 403
        return f(current_user, *args, **kwargs)

    return decorated


def owner_or_admin_required(f):
    @wraps(f)
    def decorated(current_user, *args, **kwargs):
        id = kwargs.get('id')
        if id is not None:
            if int(current_user.id) != id and current_user.role != 'admin':
                return jsonify({'message': 'Permission denied'}), 403
        return f(current_user, *args, **kwargs)

    return decorated
