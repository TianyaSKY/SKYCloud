"""File and folder change log entries used by organize scans and audits."""

from datetime import datetime
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infra.datetime_utils import local_isoformat, utc_now
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime

if TYPE_CHECKING:
    from app.models.workspace import Workspace


class FileChangeEventDict(TypedDict):
    """Public representation of a file change event row."""

    id: int
    workspace_id: int
    entity_type: str
    entity_id: int
    action: str
    old_parent_id: int | None
    new_parent_id: int | None
    old_name: str | None
    new_name: str | None
    payload: str | None
    created_at: str | None


class FileChangeEvent(Base):
    """Change log entry for incremental organize scans and audits."""

    __tablename__ = "file_change_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("workspaces.id"),
        nullable=False,
        index=True,
    )
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    old_parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    old_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    new_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        nullable=False,
        index=True,
    )

    workspace: Mapped["Workspace"] = relationship(
        "Workspace",
        backref="file_change_events",
    )

    def to_dict(self) -> FileChangeEventDict:
        """Return the public change event representation."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "action": self.action,
            "old_parent_id": self.old_parent_id,
            "new_parent_id": self.new_parent_id,
            "old_name": self.old_name,
            "new_name": self.new_name,
            "payload": self.payload,
            "created_at": local_isoformat(self.created_at),
        }
