import logging
import os
from datetime import datetime

from flask import Blueprint, request, jsonify, send_file

from app.services import share_service
from app.utils.decorators import token_required

logger = logging.getLogger(__name__)

share_bp = Blueprint('share', __name__)


@share_bp.route('/share', methods=['POST'])
@token_required
def create_share(current_user):
    data = request.get_json()
    file_id = data.get('file_id')
    expires_at_str = data.get('expires_at')  # 格式如 "2023-12-31T23:59:59"  到期时间

    expires_at = None
    if expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
        except ValueError:
            return jsonify({'error': 'Invalid date format'}), 400

    try:
        share = share_service.create_share_link(current_user.id, file_id, expires_at)
        return jsonify(share.to_dict()), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 404
    except Exception as e:
        logger.error(f"Create share error: {e}")
        return jsonify({'error': str(e)}), 500


@share_bp.route('/share/<token>', methods=['GET'])
def access_share(token):
    """
    公开访问接口，无需登录
    """
    share = share_service.get_share_by_token(token)
    if not share:
        return jsonify({'error': 'Link invalid or expired'}), 404

    file = share.file
    if not file or not file.file_path or not os.path.exists(file.file_path):
        return jsonify({'error': 'File not found'}), 404

    # 直接返回文件流，不强制下载，以便在浏览器中预览（如头像显示）
    abs_path = os.path.abspath(file.file_path)
    return send_file(
        abs_path,
        as_attachment=False,
        download_name=file.name,
        mimetype=file.mime_type
    )


@share_bp.route('/share/my', methods=['GET'])
@token_required
def get_my_shares(current_user):
    """
    获取当前用户的所有分享
    """
    try:
        shares = share_service.get_my_shares(current_user.id)
        return jsonify(shares), 200
    except Exception as e:
        logger.error(f"Get my shares error: {e}")
        return jsonify({'error': str(e)}), 500


@share_bp.route('/share/<int:share_id>', methods=['DELETE'])
@token_required
def cancel_share(current_user, share_id):
    """
    取消分享
    """
    try:
        success = share_service.cancel_share(share_id, current_user.id)
        if success:
            return jsonify({'message': 'Share cancelled successfully'}), 200
        else:
            return jsonify({'error': 'Share not found or permission denied'}), 404
    except Exception as e:
        logger.error(f"Cancel share error: {e}")
        return jsonify({'error': str(e)}), 500
