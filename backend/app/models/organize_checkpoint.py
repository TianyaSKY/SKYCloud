"""Organize progress checkpoints per workspace."""

from datetime import datetime
from typing import TYPE_CHECKING, TypedDict

from sqlalchemy import ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infra.datetime_utils import local_isoformat, utc_now
from app.infra.extensions import Base
from app.infra.utc_datetime import UTCDateTime

if TYPE_CHECKING:
    from app.models.workspace import Workspace


class OrganizeCheckpointDict(TypedDict):
    """Public representation of an organize checkpoint row."""

    workspace_id: int
    last_event_id: int
    last_full_scan_at: str | None
    updated_at: str | None


class OrganizeCheckpoint(Base):
    """Consumed change-event position and last full scan per workspace."""

    __tablename__ = "organize_checkpoints"

    workspace_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("workspaces.id"),
        primary_key=True,
    )
    last_event_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    last_full_scan_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        nullable=True,
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(),
        default=utc_now,
        onupdate=utc_now,
    )

    workspace: Mapped["Workspace"] = relationship(
        "Workspace",
        backref="organize_checkpoint",
    )

    def to_dict(self) -> OrganizeCheckpointDict:
        """Return the public checkpoint representation."""
        return {
            "workspace_id": self.workspace_id,
            "last_event_id": self.last_event_id,
            "last_full_scan_at": local_isoformat(self.last_full_scan_at),
            "updated_at": local_isoformat(self.updated_at),
        }
