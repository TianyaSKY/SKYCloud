"""仅限 SQL 的远程清理暂存与准入防线."""

from sqlalchemy.orm import Session

from app.models.assistant import AssistantConversation, AssistantRun
from app.models.assistant_runtime_abort import AssistantRuntimeAbort
from app.models.opencode_runtime import OpenCodeRuntime
from app.models.runtime_operation import RuntimeOperation


def stage(session: Session, run: AssistantRun) -> None:
    """为已取消或失败的运行暂存一次幂等的远程清理."""
    if run.engine != "expert" or (
        not run.opencode_session_id and not run.runtime_lease_token
    ):
        return
    if session.get(AssistantRuntimeAbort, run.id) is not None:
        return
    conversation = session.get(AssistantConversation, run.conversation_id)
    if conversation is None:
        return
    runtime_id = run.runtime_lease_runtime_id or conversation.opencode_runtime_id
    runtime = session.get(OpenCodeRuntime, runtime_id) if runtime_id else None
    container_id = run.runtime_lease_container_id or (
        runtime.container_id if runtime else None
    )
    if runtime_id is None or not container_id:
        return  # 游离或已删除的运行时资源使用清理 outbox.
    if runtime is not None and (
        runtime.user_id != conversation.user_id
        or runtime.workspace_id != conversation.workspace_id
    ):
        message = "Assistant runtime ownership mismatch"
        raise ValueError(message)
    generation: str | None = (
        session.query(RuntimeOperation.generation)
        .filter(
            RuntimeOperation.candidate_container_id == container_id,
            RuntimeOperation.runtime_id == runtime_id,
        )
        .scalar()
    )
    session.add(
        AssistantRuntimeAbort(
            run_id=run.id,
            workspace_id=conversation.workspace_id,
            user_id=conversation.user_id,
            runtime_id=runtime_id,
            container_id=container_id,
            generation=generation,
            session_id=run.opencode_session_id,
            lease_token=run.runtime_lease_token,
        ),
    )


def pending_for_member(session: Session, workspace_id: int, user_id: int) -> bool:
    """返回指定成员是否仍持有未完成的远程清理."""
    return (
        session.query(AssistantRuntimeAbort.run_id)
        .filter(
            AssistantRuntimeAbort.workspace_id == workspace_id,
            AssistantRuntimeAbort.user_id == user_id,
            AssistantRuntimeAbort.completed_at.is_(None),
        )
        .first()
        is not None
    )
