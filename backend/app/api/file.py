import os

from flask import request, jsonify, send_file

from app.api import api_bp
from app.services import file_service
from app.utils.decorators import token_required, owner_or_admin_required


@api_bp.route('/files', methods=['POST'])
@token_required
def create_file(current_user):
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    # 获取其他表单数据
    data = request.form.to_dict()

    # 使用当前登录用户的 ID 作为 uploader_id
    data['uploader_id'] = current_user.id

    try:
        new_file = file_service.create_file(file, data)
        return jsonify(new_file.to_dict()), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@api_bp.route('/files/batch', methods=['POST'])
@token_required
def batch_upload_files(current_user):
    # 前端需使用 formData.append('files', file) 多次添加文件
    files = request.files.getlist('files')
    if not files or (len(files) == 1 and files[0].filename == ''):
        return jsonify({'error': 'No selected files'}), 400

    data = request.form.to_dict()
    data['uploader_id'] = current_user.id

    try:
        new_files = file_service.batch_create_files(files, data)
        return jsonify([f.to_dict() for f in new_files]), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@api_bp.route('/files/<int:id>', methods=['GET'])
@token_required
def get_file(current_user, id):
    file = file_service.get_file(id)
    return jsonify(file.to_dict())


@api_bp.route('/files/list', methods=['GET'])
@token_required
def list_files(current_user):
    parent_id = request.args.get('parent_id', type=int)
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 10, type=int)
    name = request.args.get('name')
    sort_by = request.args.get('sort_by', 'created_at')
    order = request.args.get('order', 'desc')

    results = file_service.get_files_and_folders(
        current_user.id,
        parent_id,
        page,
        page_size,
        name,
        sort_by,
        order
    )
    return jsonify(results)


@api_bp.route('/files/search', methods=['GET'])
@token_required
def search_files(current_user):
    query = request.args.get('q', '')
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 10, type=int)
    search_type = request.args.get('type', 'fuzzy')

    if not query:
        return jsonify({'items': [], 'total': 0, 'page': page, 'page_size': page_size})

    results = file_service.search_files(current_user.id, query, page, page_size, search_type)
    return jsonify(results)


@api_bp.route('/files/<int:id>', methods=['PUT'])
@token_required
def update_file(current_user, id):
    data = request.get_json()
    file = file_service.update_file(id, data)
    return jsonify(file.to_dict())


@api_bp.route('/files/<int:id>', methods=['DELETE'])
@token_required
def delete_file(current_user, id):
    file_service.delete_file(id)
    return '', 204


@api_bp.route('/files/<int:id>/download', methods=['GET'])
@token_required
def download_file(current_user, id):
    try:
        file = file_service.get_file(id)
        abs_path = file.get_abs_path()

        # 检查文件是否存在
        if not os.path.exists(abs_path):
            return jsonify({'error': 'File not found on server'}), 404

        return send_file(
            abs_path,
            as_attachment=True,
            download_name=file.name,
            mimetype=file.mime_type
        )
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@api_bp.route('/files/upload/avatar/<int:id>', methods=['POST'])
@token_required
@owner_or_admin_required
def upload_avatar(current_user, id):
    if 'avatar' not in request.files:
        return jsonify({'error': 'No avatar part'}), 400
    avatar = request.files['avatar']
    return jsonify(file_service.upload_avatar(avatar, id), 200)


@api_bp.route('/files/batch-delete', methods=['POST'])
@token_required
def batch_delete_files(current_user):
    data = request.get_json()
    items = data.get("items", [])
    file_service.batch_delete_items(items)
    return '', 204


@api_bp.route('/files/retry_embedding', methods=['POST'])
@token_required
def retry_embedding(current_user):
    # 尝试对某个文件重新建立索引
    data = request.get_json()
    file_id = data.get("file_id")
    file_service.retry_embedding(file_id)
    return '', 204


@api_bp.route('/files/rebuild_failed_indexes', methods=['POST'])
@token_required
def rebuild_failed_indexes(current_user):
    count = file_service.rebuild_failed_indexes(current_user.id)
    return jsonify({'count': count}), 200


@api_bp.route('/files/process_status',methods=['GET'])
@token_required
def process_status(current_user):
    return jsonify(file_service.process_status(current_user.id))