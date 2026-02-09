from sqlalchemy import Column, Integer, String, Boolean

from app.extensions import Base, _scoped_session


class SysDict(Base):
    __tablename__ = "sys_dict"

    id = Column(Integer, primary_key=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(String(255), nullable=False)
    des = Column(String(2048))
    enable = Column(Boolean, default=True)

    # 添加 query 属性用于兼容 Flask-SQLAlchemy 风格的查询
    query = _scoped_session.query_property()

    def to_dict(self):
        return {
            "id": self.id,
            "key": self.key,
            "value": self.value,
            "enable": self.enable,
            "des": self.des,
        }

    @classmethod
    def from_cache(cls, d: dict) -> "SysDict":
        return cls(
            id=d.get("id"),
            key=d.get("key"),
            value=d.get("value"),
            enable=d.get("enable"),
            des=d.get("des"),
        )
