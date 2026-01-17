import logging

from flask import Blueprint, request, jsonify

from app.services.auth_service import authenticate_user
from app.services.user_service import create_user

logger = logging.getLogger(__name__)

auth_bp = Blueprint('auth', __name__)


@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    if not data or not data.get('username') or not data.get('password'):
        return jsonify({'message': 'Missing username or password'}), 400

    token, role, user_id = authenticate_user(data['username'], data['password'])
    if token:
        return jsonify({'token': token, 'message': 'Login successful', 'user': role, 'user_id': user_id}), 200
    else:
        return jsonify({'message': 'Invalid username or password'}), 401


@auth_bp.route('/register', methods=['POST'])
def register():
    data = request.get_json()
    if not data or not data.get('username') or not data.get('password'):
        return jsonify({'message': 'Missing username or password'}), 400

    try:
        user = create_user(data)
        return jsonify({'message': 'User registered successfully', 'user': user.to_dict()}), 201
    except Exception as e:
        logger.error(f"Registration error: {e}")
        return jsonify({'message': str(e)}), 400
