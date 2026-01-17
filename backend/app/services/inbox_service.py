from app.extensions import db
from app.models.inbox import Inbox


def create_inbox_message(data):
    new_message = Inbox(
        user_id=data.get('user_id'),
        title=data.get('title'),
        content=data.get('content'),
        type=data.get('type', 'system')
    )
    db.session.add(new_message)
    db.session.commit()
    return new_message


def get_user_inbox(user_id, page=1, per_page=20):
    pagination = Inbox.query.filter_by(user_id=user_id, is_deleted=False) \
        .order_by(Inbox.created_at.desc()) \
        .paginate(page=page, per_page=per_page, error_out=False)
    return pagination


def get_inbox_message(id):
    return Inbox.query.get_or_404(id)


def mark_as_read(id, user_id):
    message = Inbox.query.filter_by(id=id, user_id=user_id).first_or_404()
    message.is_read = True
    db.session.commit()
    return message


def delete_inbox_message(id, user_id):
    message: Inbox = Inbox.query.filter_by(id=id, user_id=user_id).first_or_404()
    message.is_deleted = True
    db.session.commit()


def mark_all_as_read(user_id):
    Inbox.query.filter_by(user_id=user_id, is_read=False).update({'is_read': True})
    db.session.commit()
