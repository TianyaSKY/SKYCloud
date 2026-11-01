"""HTTP avatar adaptation and SQL lifecycle owned by a single worker."""

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
    """Check the actor role, then run the avatar upload on the SQL worker.

    Callers must provide either ``upload`` or the base64 ``encoded`` payload.
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
