"""收件箱路由:系统通知的分页、已读与删除.业务在 inbox_service."""

from collections.abc import Mapping
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response, status

from app.api.contracts import SuccessEnvelope, SuccessResponse, success_payload
from app.api.dependencies import get_current_user
from app.features.inbox import service as inbox_service
from app.features.inbox.schemas import (
    InboxAcknowledgment,
    InboxMessage,
    InboxPage,
)
from app.infra.extensions import SessionFactory, get_session_factory
from app.models.user import User

router = APIRouter(tags=["inbox"])

SessionFactoryDep = Annotated[SessionFactory, Depends(get_session_factory)]
CurrentUser = Annotated[User, Depends(get_current_user)]
PageQuery = Annotated[int, Query(ge=1)]
PerPageQuery = Annotated[int, Query(ge=1, le=100)]


@router.get("/inbox", response_model=SuccessResponse[InboxPage])
def get_inbox(
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
    page: PageQuery = 1,
    per_page: PerPageQuery = 20,
) -> SuccessEnvelope[Mapping[str, object]]:
    """分页获取当前用户收件箱消息."""
    with session_factory() as session:
        result = inbox_service.get_user_inbox(
            session, current_user.id, page, per_page,
        )

        return success_payload(
            {
                "items": [item.to_dict() for item in result["items"]],
                "total": result["total"],
                "pages": result["pages"],
                "current_page": result["page"],
            },
        )


@router.put("/inbox/{id}/read", response_model=SuccessResponse[InboxMessage])
def mark_message_read(
    message_id: Annotated[int, Path(alias="id")],
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[Mapping[str, object]]:
    """标记单条消息为已读(仅本人消息)."""
    with session_factory() as session:
        message = inbox_service.mark_as_read(session, message_id, current_user.id)
        return success_payload(message.to_dict())


@router.put("/inbox/read-all", response_model=SuccessResponse[InboxAcknowledgment])
def mark_all_messages_read(
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> SuccessEnvelope[Mapping[str, object]]:
    """将当前用户全部未读标记为已读."""
    with session_factory() as session:
        inbox_service.mark_all_as_read(session, current_user.id)
        return success_payload({"message": "All messages marked as read"})


@router.delete("/inbox/{id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_message(
    message_id: Annotated[int, Path(alias="id")],
    current_user: CurrentUser,
    session_factory: SessionFactoryDep,
) -> Response:
    """删除单条消息(仅本人)."""
    with session_factory() as session:
        inbox_service.delete_inbox_message(session, message_id, current_user.id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
