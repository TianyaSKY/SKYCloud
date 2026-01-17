from flask import request, jsonify

from app.api import api_bp
from app.services import inbox_service
from app.utils.decorators import token_required


@api_bp.route('/inbox', methods=['GET'])
@token_required
def get_inbox(current_user):
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)

    pagination = inbox_service.get_user_inbox(current_user.id, page, per_page)

    return jsonify({
        'items': [item.to_dict() for item in pagination.items],
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': pagination.page
    })


@api_bp.route('/inbox/<int:id>/read', methods=['PUT'])
@token_required
def mark_message_read(current_user, id):
    message = inbox_service.mark_as_read(id, current_user.id)
    return jsonify(message.to_dict())


@api_bp.route('/inbox/read-all', methods=['PUT'])
@token_required
def mark_all_messages_read(current_user):
    inbox_service.mark_all_as_read(current_user.id)
    return jsonify({'message': 'All messages marked as read'})


@api_bp.route('/inbox/<int:id>', methods=['DELETE'])
@token_required
def delete_message(current_user, id):
    inbox_service.delete_inbox_message(id, current_user.id)
    return '', 204
