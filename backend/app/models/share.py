import uuid
from datetime import datetime

from app.extensions import db


class Share(db.Model):
    __tablename__ = 'shares'

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(512), unique=True, nullable=False, default=lambda: str(uuid.uuid4()))
    file_id = db.Column(db.Integer, db.ForeignKey('files.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=True)  # 可选：过期时间

    file = db.relationship('File', back_populates='shares')
    user = db.relationship('User')

    def to_dict(self):
        return {
            'id': self.id,
            'token': self.token,
            'file_id': self.file_id,
            'file_name': self.file.name if self.file else None,
            'created_at': self.created_at.isoformat(),
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'link': f'/api/share/{self.token}'
        }

    @classmethod
    def from_cache(cls, d: dict) -> 'Share':
        return cls(
            id=d.get('id'),
            token=d.get('token'),
            file_id=d.get('file_id'),
            created_at=datetime.fromisoformat(d.get('created_at')) if d.get('created_at') else None,
            expires_at=datetime.fromisoformat(d.get('expires_at')) if d.get('expires_at') else None
        )
