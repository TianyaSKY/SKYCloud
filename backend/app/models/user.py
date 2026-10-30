"""User account rows: login identity, role, and accumulated token usage."""

from datetime import datetime
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy import BigInteger, Enum, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from werkzeug.security import check_password_hash, generate_password_hash

from app.infra.datetime_utils import (
    local_isoformat,
    parse_legacy_datetime,
    utc_now,
)
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime

if TYPE_CHECKING:
    from app.api.stream_credentials import StreamCredential


class UserDict(TypedDict):
    """Public representation of a user account row."""

    id: int
    username: str
    role: str | None
    avatar: str | None
    created_at: str | None
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    last_active_at: str | None


class User(Base):
    """User account row: login identity, role, and token usage totals."""

    __tablename__ = "users"

    # The request-local stream credential is declared but never mapped.
    __allow_unmapped__ = True

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(1024), nullable=False)
    auth_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
    role: Mapped[str | None] = mapped_column(
        Enum(
            "admin",
            "common",
            name="user_roles",
            native_enum=False,
            length=10,
        ),
        default="common",
    )
    avatar: Mapped[str | None] = mapped_column(String(255), default=None)
    created_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=utc_now,
    )

    # ---- Token usage totals, written from LLM call details ----
    total_prompt_tokens: Mapped[int | None] = mapped_column(
        BigInteger,
        default=0,
    )
    total_completion_tokens: Mapped[int | None] = mapped_column(
        BigInteger,
        default=0,
    )
    total_tokens: Mapped[int | None] = mapped_column(BigInteger, default=0)
    last_active_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=None,
    )

    # Request-local credential for continued stream authorization. It is set
    # by the HTTP credential dependency and never persisted or serialized.
    http_stream_credential: "StreamCredential | None" = None

    def __repr__(self) -> str:
        """Return a debug representation of this user."""
        return f"<User {self.username}>"

    def set_password(self, password: str) -> None:
        """Store a password hash for the given plaintext password."""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        """Return whether the password matches the stored hash."""
        return check_password_hash(self.password_hash, password)

    def to_dict(self) -> UserDict:
        """Return the public user representation."""
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "avatar": self.avatar,
            "created_at": local_isoformat(self.created_at),
            "total_prompt_tokens": self.total_prompt_tokens or 0,
            "total_completion_tokens": self.total_completion_tokens or 0,
            "total_tokens": self.total_tokens or 0,
            "last_active_at": local_isoformat(self.last_active_at),
        }

    @classmethod
    def from_cache(cls, d: UserDict) -> "User":
        """Build a detached user from cached profile fields."""
        return cls(
            id=d.get("id"),
            username=d.get("username"),
            role=d.get("role"),
            avatar=d.get("avatar"),
            created_at=parse_legacy_datetime(d.get("created_at")),
            total_prompt_tokens=d.get("total_prompt_tokens", 0),
            total_completion_tokens=d.get("total_completion_tokens", 0),
            total_tokens=d.get("total_tokens", 0),
            last_active_at=parse_legacy_datetime(d.get("last_active_at")),
        )
