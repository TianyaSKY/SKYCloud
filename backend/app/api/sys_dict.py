from flask import request, jsonify

from app.api import api_bp
from app.services import sys_dict_service
from app.utils.decorators import token_required, admin_required


@api_bp.route('/sys_dicts', methods=['POST'])
@token_required
@admin_required
def create_sys_dict(current_user):
    data = request.get_json()
    sys_dict = sys_dict_service.create_sys_dict(data)
    return jsonify(sys_dict.to_dict()), 201


@api_bp.route('/sys_dicts/<int:id>', methods=['GET'])
@token_required
@admin_required
def get_sys_dict(current_user, id):
    sys_dict_data = sys_dict_service.get_sys_dict(id)
    return jsonify(sys_dict_data)


@api_bp.route('/sys_dicts/<int:id>', methods=['PUT'])
@token_required
@admin_required
def update_sys_dict(current_user, id):
    data = request.get_json()
    sys_dict = sys_dict_service.update_sys_dict(id, data)
    return jsonify(sys_dict.to_dict())


@api_bp.route('/sys_dicts/<int:id>', methods=['DELETE'])
@token_required
@admin_required
def delete_sys_dict(current_user, id):
    sys_dict_service.delete_sys_dict(id)
    return '', 204


@api_bp.route('/sys_dicts', methods=['GET'])
@token_required
@admin_required
def get_sys_dicts(current_user):
    return jsonify(sys_dict_service.get_sys_dict_all())
