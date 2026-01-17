from app.extensions import db


class Folder(db.Model):
    __tablename__ = 'folder'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'))
    parent_id = db.Column(db.Integer, db.ForeignKey('folder.id'))

    # 递归建立子文件夹关系，并设置级联删除
    sub_folder = db.relationship('Folder', backref=db.backref('parent', remote_side=[id]), cascade='all, delete-orphan')

    # 级联删除：当文件夹被删除时，自动删除其下的文件
    # 注意：这里只处理数据库层面的级联删除，物理文件的删除仍需在 service 层处理
    files = db.relationship('File', backref='folder', cascade='all, delete-orphan')

    def to_dict(self):
        path_parts = []
        current = self
        while current:
            if current.parent_id is None:
                break
            path_parts.append(current.name)
            current = current.parent

        path = '/' + '/'.join(reversed(path_parts))

        return {
            'id': self.id,
            'name': '/' if self.parent_id is None else self.name,
            'user_id': self.user_id,
            'parent_id': self.parent_id,
            'path': path
        }

    @classmethod
    def from_cache(cls, d: dict) -> 'Folder':
        return cls(
            id=d.get('id'),
            name=d.get('name'),
            user_id=d.get('user_id'),
            parent_id=d.get('parent_id')
        )
