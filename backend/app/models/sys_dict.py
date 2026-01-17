from app.extensions import db


class SysDict(db.Model):
    __tablename__ = 'sys_dict'

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.String(255), nullable=False)
    des = db.Column(db.String(2048))
    enable = db.Column(db.Boolean, default=True)

    def to_dict(self):
        return {
            'id': self.id,
            'key': self.key,
            'value': self.value,
            'enable': self.enable,
            'des': self.des
        }

    @classmethod
    def from_cache(cls, d: dict) -> 'SysDict':
        return cls(
            id=d.get('id'),
            key=d.get('key'),
            value=d.get('value'),
            enable=d.get('enable'),
            des=d.get('des')
        )
