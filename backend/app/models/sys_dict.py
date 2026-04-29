from sqlalchemy import Column, Integer, String, Boolean

from app.extensions import Base


class SysDict(Base):
    __tablename__ = "sys_dict"

    id = Column(Integer, primary_key=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(String(255), nullable=False)
    des = Column(String(2048))
    enable = Column(Boolean, default=True)



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
