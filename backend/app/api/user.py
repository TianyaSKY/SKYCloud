from flask import request, jsonify

from app.api import api_bp
from app.services import user_service
from app.utils.decorators import token_required, owner_or_admin_required


@api_bp.route('/users', methods=['POST'])
def create_user():
    data = request.get_json()
    user = user_service.create_user(data)
    return jsonify(user.to_dict()), 201


@api_bp.route('/users/<int:id>', methods=['GET'])
@token_required
@owner_or_admin_required
def get_user(current_user, id):
    # Optional: Check if current_user.id == id or if current_user is admin
    user = user_service.get_user(id)

    return jsonify(user.to_dict())


@api_bp.route('/users/<int:id>', methods=['PUT'])
@token_required
@owner_or_admin_required
def update_user(current_user, id):
    data = request.get_json()
    user = user_service.update_user(id, data)
    return jsonify(user.to_dict())


@api_bp.route('/users/<int:id>', methods=['DELETE'])
@token_required
@owner_or_admin_required
def delete_user(current_user, id):
    user_service.delete_user(id)
    return '', 204
