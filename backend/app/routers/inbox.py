from fastapi import APIRouter, Depends, Response, status

from app.dependencies import get_current_user
from app.services import inbox_service

router = APIRouter(tags=["inbox"])


@router.get("/inbox")
def get_inbox(
        current_user=Depends(get_current_user), page: int = 1, per_page: int = 20
):
    result = inbox_service.get_user_inbox(current_user.id, page, per_page)

    return {
        "items": [item.to_dict() for item in result["items"]],
        "total": result["total"],
        "pages": result["pages"],
        "current_page": result["page"],
    }


@router.put("/inbox/{id}/read")
def mark_message_read(id: int, current_user=Depends(get_current_user)):
    message = inbox_service.mark_as_read(id, current_user.id)
    return message.to_dict()


@router.put("/inbox/read-all")
def mark_all_messages_read(current_user=Depends(get_current_user)):
    inbox_service.mark_all_as_read(current_user.id)
    return {"message": "All messages marked as read"}


@router.delete("/inbox/{id}")
def delete_message(id: int, current_user=Depends(get_current_user)):
    inbox_service.delete_inbox_message(id, current_user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
