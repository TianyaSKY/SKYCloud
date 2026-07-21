"""收件箱路由：系统通知的分页、已读与删除。业务在 inbox_service。"""

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.extensions import get_db
from app.services import inbox_service

router = APIRouter(tags=["inbox"])


@router.get("/inbox")
def get_inbox(
        current_user=Depends(get_current_user),
        session: Session = Depends(get_db),
        page: int = Query(default=1, ge=1),
        per_page: int = Query(default=20, ge=1, le=100),
):
    """分页获取当前用户收件箱消息。"""
    result = inbox_service.get_user_inbox(session, current_user.id, page, per_page)

    return {
        "items": [item.to_dict() for item in result["items"]],
        "total": result["total"],
        "pages": result["pages"],
        "current_page": result["page"],
    }


@router.put("/inbox/{id}/read")
def mark_message_read(
    id: int,
    current_user=Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """标记单条消息为已读（仅本人消息）。"""
    message = inbox_service.mark_as_read(session, id, current_user.id)
    return message.to_dict()


@router.put("/inbox/read-all")
def mark_all_messages_read(
    current_user=Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """将当前用户全部未读标记为已读。"""
    inbox_service.mark_all_as_read(session, current_user.id)
    return {"message": "All messages marked as read"}


@router.delete("/inbox/{id}")
def delete_message(
    id: int,
    current_user=Depends(get_current_user),
    session: Session = Depends(get_db),
):
    """删除单条消息（仅本人）。"""
    inbox_service.delete_inbox_message(session, id, current_user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
