"""HTTP 头像适配与单 Worker 持有的 SQL 生命周期."""

from fastapi import UploadFile

from app.exceptions import PermissionDeniedError
from app.features.auth import account_repository
from app.features.file.avatar_service import upload_avatar_for_user
from app.infra.extensions import SessionFactory
from app.infra.upload_adapter import Base64UploadAdapter, FastAPIUploadAdapter


def upload_avatar_owned(
    session_factory: SessionFactory,
    actor_id: int,
    user_id: int,
    *,
    upload: UploadFile | None = None,
    encoded: str | None = None,
) -> dict[str, object]:
    """校验操作者角色, 并在 SQL Worker 上执行头像上传.

    调用方必须提供 ``upload`` 文件对象或 Base64 ``encoded`` 载荷。
    """
    with session_factory() as session:
        role = account_repository.current_role(session, actor_id)
        if role is None or (actor_id != user_id and role != "admin"):
            message = "Permission denied"
            raise PermissionDeniedError(message)
        adapter: FastAPIUploadAdapter | Base64UploadAdapter
        if upload is not None:
            adapter = FastAPIUploadAdapter(upload)
        else:
            adapter = Base64UploadAdapter(encoded or "")
        return upload_avatar_for_user(
            session, actor_id, role, user_id, adapter,
        )
