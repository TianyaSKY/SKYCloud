"""Persistent conversations, messages, and runs for the SKYCloud assistant.

The assistant deliberately keeps a conversation's mode immutable.  A fast
conversation is backed by the read-only RAG engine, while an expert
conversation is backed by an OpenCode session.  Keeping the records in the
database lets the UI recover after a browser refresh or an interrupted SSE
connection without mixing the two execution models.
"""

import json
from datetime import datetime
from typing import Any, cast

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.infra.datetime_utils import beijing_now, local_isoformat
from app.infra.extensions import Base


def _load_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class AssistantConversation(Base):
    """A mode-specific assistant thread owned by one workspace member."""

    __tablename__ = "assistant_conversations"

    id = Column(Integer, primary_key=True)
    workspace_id = Column(
        Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    mode = Column(String(16), nullable=False, default="fast")
    title = Column(String(255), nullable=True)
    status = Column(String(16), nullable=False, default="active")
    source_conversation_id = Column(
        Integer,
        ForeignKey("assistant_conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    opencode_runtime_id = Column(
        Integer,
        ForeignKey("opencode_runtimes.id", ondelete="SET NULL"),
        nullable=True,
    )
    opencode_session_id = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=beijing_now, nullable=False)
    updated_at = Column(DateTime, default=beijing_now, onupdate=beijing_now, nullable=False)
    last_message_at = Column(DateTime, nullable=True)

    workspace = relationship("Workspace", backref="assistant_conversations")
    user = relationship("User", backref="assistant_conversations")
    messages = relationship(
        "AssistantMessage",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="AssistantMessage.sequence",
    )
    runs = relationship(
        "AssistantRun",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="AssistantRun.created_at",
    )
    source_conversation = relationship(
        "AssistantConversation",
        remote_side=[id],
        foreign_keys=[source_conversation_id],
        backref="handoff_conversations",
    )

    __table_args__ = (
        CheckConstraint("mode IN ('fast', 'expert')", name="ck_assistant_conversation_mode"),
        CheckConstraint("status IN ('active', 'archived')", name="ck_assistant_conversation_status"),
        Index("idx_assistant_conversations_owner", "workspace_id", "user_id", "mode"),
        Index("idx_assistant_conversations_updated", "workspace_id", "user_id", "updated_at"),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "mode": self.mode,
            "title": self.title,
            "status": self.status,
            "source_conversation_id": self.source_conversation_id,
            "opencode_runtime_id": self.opencode_runtime_id,
            "opencode_session_id": self.opencode_session_id,
            "created_at": local_isoformat(cast(datetime | None, self.created_at)),
            "updated_at": local_isoformat(cast(datetime | None, self.updated_at)),
            "last_message_at": local_isoformat(cast(datetime | None, self.last_message_at)),
        }


class AssistantMessage(Base):
    """A durable user, assistant, or system message."""

    __tablename__ = "assistant_messages"

    id = Column(Integer, primary_key=True)
    conversation_id = Column(
        Integer,
        ForeignKey("assistant_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role = Column(String(16), nullable=False)
    content = Column(Text, nullable=False, default="")
    status = Column(String(16), nullable=False, default="completed")
    provider_message_id = Column(String(128), nullable=True)
    metadata_json = Column(Text, nullable=True)
    usage_json = Column(Text, nullable=True)
    sequence = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=beijing_now, nullable=False)
    updated_at = Column(DateTime, default=beijing_now, onupdate=beijing_now, nullable=False)

    conversation = relationship("AssistantConversation", back_populates="messages")

    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant', 'system')", name="ck_assistant_message_role"),
        CheckConstraint(
            "status IN ('pending', 'streaming', 'completed', 'failed', 'cancelled')",
            name="ck_assistant_message_status",
        ),
        UniqueConstraint("conversation_id", "sequence", name="uq_assistant_message_sequence"),
        Index("idx_assistant_messages_conversation", "conversation_id", "sequence"),
    )

    @property
    def metadata_dict(self) -> dict[str, Any]:
        value = _load_json(self.metadata_json, {})
        return value if isinstance(value, dict) else {}

    @metadata_dict.setter
    def metadata_dict(self, value: dict[str, Any] | None) -> None:
        self.metadata_json = _dump_json(value or {})

    @property
    def usage(self) -> dict[str, Any]:
        value = _load_json(self.usage_json, {})
        return value if isinstance(value, dict) else {}

    @usage.setter
    def usage(self, value: dict[str, Any] | None) -> None:
        self.usage_json = _dump_json(value or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "role": self.role,
            "content": self.content,
            "status": self.status,
            "provider_message_id": self.provider_message_id,
            "metadata": self.metadata_dict,
            "usage": self.usage,
            "sequence": self.sequence,
            "created_at": local_isoformat(cast(datetime | None, self.created_at)),
            "updated_at": local_isoformat(cast(datetime | None, self.updated_at)),
        }


class AssistantRun(Base):
    """One execution attempt for one user message."""

    __tablename__ = "assistant_runs"

    id = Column(Integer, primary_key=True)
    conversation_id = Column(
        Integer,
        ForeignKey("assistant_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_message_id = Column(
        Integer,
        ForeignKey("assistant_messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    assistant_message_id = Column(
        Integer,
        ForeignKey("assistant_messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    engine = Column(String(16), nullable=False)
    status = Column(String(24), nullable=False, default="pending")
    request_id = Column(String(128), nullable=True)
    opencode_session_id = Column(String(128), nullable=True)
    pending_permission_id = Column(String(128), nullable=True)
    permission_response = Column(String(16), nullable=True)
    cancel_requested = Column(Boolean, nullable=False, default=False)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    metadata_json = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=beijing_now, nullable=False)

    conversation = relationship("AssistantConversation", back_populates="runs")
    user_message = relationship("AssistantMessage", foreign_keys=[user_message_id])
    assistant_message = relationship("AssistantMessage", foreign_keys=[assistant_message_id])

    __table_args__ = (
        CheckConstraint("engine IN ('fast', 'expert')", name="ck_assistant_run_engine"),
        CheckConstraint(
            "status IN ('pending', 'running', 'waiting_permission', 'completed', 'failed', 'cancelled')",
            name="ck_assistant_run_status",
        ),
        CheckConstraint(
            "permission_response IS NULL OR permission_response IN ('once', 'reject')",
            name="ck_assistant_run_permission_response",
        ),
        Index("idx_assistant_runs_conversation", "conversation_id", "created_at"),
        Index("idx_assistant_runs_active", "status", "engine"),
    )

    @property
    def metadata_dict(self) -> dict[str, Any]:
        value = _load_json(self.metadata_json, {})
        return value if isinstance(value, dict) else {}

    @metadata_dict.setter
    def metadata_dict(self, value: dict[str, Any] | None) -> None:
        self.metadata_json = _dump_json(value or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "user_message_id": self.user_message_id,
            "assistant_message_id": self.assistant_message_id,
            "engine": self.engine,
            "status": self.status,
            "request_id": self.request_id,
            "opencode_session_id": self.opencode_session_id,
            "pending_permission_id": self.pending_permission_id,
            "cancel_requested": self.cancel_requested,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "metadata": self.metadata_dict,
            "started_at": local_isoformat(cast(datetime | None, self.started_at)),
            "completed_at": local_isoformat(cast(datetime | None, self.completed_at)),
            "created_at": local_isoformat(cast(datetime | None, self.created_at)),
        }


__all__ = ["AssistantConversation", "AssistantMessage", "AssistantRun"]
