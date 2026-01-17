from flask import request, jsonify

from app.api import api_bp
from app.services import folder_service
from app.utils.decorators import token_required, owner_or_admin_required


@api_bp.route('/folder', methods=['POST'])
@token_required
def create_folder(current_user):
    data = request.get_json()
    data['user_id'] = current_user.id
    folder = folder_service.create_folder(data)
    return jsonify(folder.to_dict()), 201


@api_bp.route('/folder/<int:id>', methods=['GET'])
@token_required
@owner_or_admin_required
def get_folder(id):
    folder = folder_service.get_folder(id)
    return jsonify(folder.to_dict())


@api_bp.route('/folder/<int:id>', methods=['PUT'])
@token_required
@owner_or_admin_required
def update_folder(current_user, id):
    data = request.get_json()
    folder = folder_service.update_folder(id, data)
    return jsonify(folder.to_dict())


@api_bp.route('/folder/<int:id>', methods=['DELETE'])
@token_required
@owner_or_admin_required
def delete_folder(current_user, id):
    folder_service.delete_folder(id)
    return '', 204


@api_bp.route('/folder/root_id', methods=['GET'])
@token_required
def get_root_folder_id(current_user):
    return jsonify({'root_folder_id': folder_service.get_root_folder_id(current_user.id), "code": 200})


@api_bp.route('/folder/all', methods=['GET'])
@token_required
def get_folders(current_user):
    return jsonify({'folders': [f.to_dict() for f in folder_service.get_folders(current_user.id)], "code": 200})


@api_bp.route('/folder/organize', methods=['POST'])
@token_required
def organize_user_files(current_user):
    """
    触发智能文件整理
    """
    folder_service.organize_files(current_user.id)
    return jsonify({'message': '已开始整理文件，请留意收件箱里的通知，将会在整理完毕后通知您', 'code': 200})
