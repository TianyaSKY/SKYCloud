from sqlalchemy import Column, Integer, String, ForeignKey
from sqlalchemy.orm import relationship, backref

from app.extensions import Base, _scoped_session


class Folder(Base):
    __tablename__ = "folder"
    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"))
    parent_id = Column(Integer, ForeignKey("folder.id"))

    # 添加 query 属性用于兼容 Flask-SQLAlchemy 风格的查询
    query = _scoped_session.query_property()

    # 递归建立子文件夹关系，并设置级联删除
    sub_folder = relationship(
        "Folder",
        backref=backref("parent", remote_side=[id]),
        cascade="all, delete-orphan",
    )

    # 级联删除：当文件夹被删除时，自动删除其下的文件
    # 注意：这里只处理数据库层面的级联删除，物理文件的删除仍需在 service 层处理
    files = relationship("File", backref="folder", cascade="all, delete-orphan")

    def to_dict(self):
        path_parts = []
        current = self
        while current:
            if current.parent_id is None:
                break
            path_parts.append(current.name)
            current = current.parent

        path = "/" + "/".join(reversed(path_parts))

        return {
            "id": self.id,
            "name": "/" if self.parent_id is None else self.name,
            "user_id": self.user_id,
            "parent_id": self.parent_id,
            "path": path,
        }

    @classmethod
    def from_cache(cls, d: dict) -> "Folder":
        return cls(
            id=d.get("id"),
            name=d.get("name"),
            user_id=d.get("user_id"),
            parent_id=d.get("parent_id"),
        )
