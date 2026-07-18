from datetime import datetime
from typing import cast

from sqlalchemy import Column, DateTime, Integer, String, ForeignKey
from sqlalchemy.orm import relationship, backref

from app.infra.datetime_utils import beijing_now, local_isoformat
from app.extensions import Base


class Folder(Base):
    __tablename__ = "folder"
    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"))
    parent_id = Column(Integer, ForeignKey("folder.id"))
    created_at = Column(DateTime, default=beijing_now)



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
            "created_at": local_isoformat(cast(datetime | None, self.created_at)),
        }

    @classmethod
    def from_cache(cls, d: dict) -> "Folder":
        return cls(
            id=d.get("id"),
            name=d.get("name"),
            user_id=d.get("user_id"),
            parent_id=d.get("parent_id"),
            created_at=datetime.fromisoformat(cast(str, d.get("created_at")))
            if cast(str | None, d.get("created_at"))
            else None,
        )
