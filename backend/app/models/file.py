import os
from datetime import datetime

from flask import current_app
from pgvector.sqlalchemy import Vector

from app.extensions import db


class File(db.Model):
    __tablename__ = 'files'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.String(512), nullable=False)  # 现在只保存文件名
    file_size = db.Column(db.BigInteger)  # 使用 BigInteger 存储字节数，方便计算容量
    mime_type = db.Column(db.String(255))  # 如 'image/jpeg', 'application/pdf'

    # AI 相关字段
    status = db.Column(db.String(20), default='pending')  # pending, processing, success, fail
    vector_info = db.Column(Vector(1536))
    description = db.Column(db.String(4096))  # 对于文件的描述

    uploader_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    parent_id = db.Column(db.Integer, db.ForeignKey('folder.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now())

    # 关系映射
    uploader = db.relationship('User', backref=db.backref('files', lazy='dynamic'))

    # 级联删除：当文件被删除时，自动删除关联的分享记录
    shares = db.relationship('Share', back_populates='file', cascade='all, delete-orphan')

    __table_args__ = (
        db.Index(
            'file_vector_idx',
            'vector_info',
            postgresql_using='hnsw',
            postgresql_ops={'vector_info': 'vector_l2_ops'}
        ),
    )

    def get_abs_path(self):
        """获取文件的绝对路径"""
        upload_folder = current_app.config.get('UPLOAD_FOLDER', '/data/uploads')
        return os.path.join(upload_folder, self.file_path)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'status': self.status,
            'description': self.description,
            'file_size': self.file_size,
            'mime_type': self.mime_type,
            'uploader_id': self.uploader_id,
            'parent_id': self.parent_id,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    @classmethod
    def from_cache(cls, d: dict) -> 'File':
        return cls(
            id=d.get('id'),
            name=d.get('name'),
            status=d.get('status'),
            description=d.get('description'),
            file_size=d.get('file_size'),
            mime_type=d.get('mime_type'),
            uploader_id=d.get('uploader_id'),
            parent_id=d.get('parent_id'),
            created_at=datetime.fromisoformat(d.get('created_at')) if d.get('created_at') else None
        )
