from datetime import datetime

from app.extensions import db


class Inbox(db.Model):
    __tablename__ = 'inbox'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    title = db.Column(db.String(255), nullable=True)
    content = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False)
    type = db.Column(db.String(50), default='system')  # e.g., 'system', 'notification'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_deleted = db.Column(db.Boolean, default=False)  # 是否删除占位

    # 关联用户
    user = db.relationship('User', backref=db.backref('inbox_messages', lazy='dynamic'))

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'title': self.title,
            'content': self.content,
            'is_read': self.is_read,
            'type': self.type,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

    @classmethod
    def from_cache(cls, d: dict) -> 'Inbox':
        return cls(
            id=d.get('id'),
            user_id=d.get('user_id'),
            title=d.get('title'),
            content=d.get('content'),
            is_read=d.get('is_read'),
            type=d.get('type'),
            created_at=datetime.fromisoformat(d.get('created_at')) if d.get('created_at') else None
        )
