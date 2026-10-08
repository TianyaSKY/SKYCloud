"""HTTP avatar adaptation and SQL lifecycle owned by a single worker."""
from app.exceptions import PermissionDeniedError
from app.features.auth import account_repository
from app.features.file import service
from app.infra.upload_adapter import Base64UploadAdapter, FastAPIUploadAdapter


def upload_avatar_owned(session_factory, actor_id, user_id, *, upload=None, encoded=None):
    with session_factory() as session:
        role = account_repository.current_role(session, actor_id)
        if role is None or (actor_id != user_id and role != 'admin'):
            raise PermissionDeniedError('Permission denied')
        adapter = FastAPIUploadAdapter(upload) if upload is not None else Base64UploadAdapter(encoded)
        return service.upload_avatar_for_user(session, actor_id, role, user_id, adapter)
