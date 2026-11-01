"""Avatar publication across account, folder, share and file use cases."""

from collections.abc import Callable

from sqlalchemy.orm import Session

from app.exceptions import (
    DomainError,
    PermissionDeniedError,
    ResourceNotFoundError,
    ServiceOperationError,
)
from app.features.auth import account_repository, user_service
from app.features.file import repository, upload_authorization
from app.features.file.deletion import clear_search_cache
from app.features.file.publication import (
    record_created,
    reserve_and_store,
    stage_record,
    stage_source,
)
from app.features.file.upload_config import UploadSettings, upload_settings
from app.features.file.upload_recovery import compensate_failed_uploads
from app.features.file.upload_types import (
    StoredUpload,
    UploadScope,
    UploadSource,
    UploadStorage,
)
from app.features.folder import service as folder_service
from app.features.share import service as share_service
from app.features.share.service import CreateShareLinkParams
from app.infra.storage import get_storage_client
from app.infra.upload_limits import AVATAR_LIMIT


class AvatarUploadService:
    """Publish an avatar object and point the account profile at its URL."""

    def __init__(
        self,
        settings: UploadSettings,
        *,
        storage_factory: Callable[[], UploadStorage] = get_storage_client,
        max_bytes: int = AVATAR_LIMIT,
    ) -> None:
        """Bind bounded staging settings and the minimal object-store boundary."""
        self.settings = settings
        self.storage_factory = storage_factory
        self.max_bytes = max_bytes

    def upload(
        self,
        session: Session,
        upload: UploadSource,
        user_id: int,
        *,
        actor_id: int | None = None,
    ) -> dict[str, object]:
        """Store the avatar and commit its file, share and account URL together."""
        authorizing_id = user_id if actor_id is None else actor_id
        user_service.authorize_account_mutation(session, authorizing_id, user_id)

        workspace = repository.find_owned_workspace(session, user_id)
        if workspace is None:
            message = "Personal workspace not found"
            raise ResourceNotFoundError(message)
        workspace_id = workspace.id
        root = repository.find_child_folder(session, workspace_id, None)
        root_id = root.id if root else None
        folder = repository.find_child_folder(
            session, workspace_id, root_id, "所有用户头像",
        )
        if folder is None:
            folder = folder_service.create_folder(
                session,
                {
                    "name": "所有用户头像",
                    "parent_id": root_id,
                    "workspace_id": workspace_id,
                },
            )

        scope = UploadScope(
            workspace_id=workspace_id,
            uploader_id=user_id,
            parent_id=folder.id,
        )
        storage = self.storage_factory()
        stored: StoredUpload | None = None
        try:
            with stage_source(
                upload, self.settings, max_bytes=self.max_bytes,
            ) as staged:
                # Keep identity authorization ahead of reservation/workspace locks.
                user_service.authorize_account_mutation(
                    session, authorizing_id, user_id,
                )
                stored = reserve_and_store(session, staged, scope, storage)
                try:
                    user_service.authorize_account_mutation(
                        session, authorizing_id, user_id,
                    )
                    upload_authorization.consume(
                        session,
                        workspace_id,
                        user_id,
                        stored.reservation_id,
                        stored.spec.file_size,
                    )
                    avatar = stage_record(session, stored.spec)
                    share = share_service.create_share_link(
                        session,
                        CreateShareLinkParams(
                            user_id=user_id, file_id=avatar.id, commit=False,
                        ),
                    )
                    avatar_url = f"/api/share/{share.token}"
                    _set_avatar_or_raise(session, user_id, avatar_url)

                    session.commit()
                except Exception:
                    compensate_failed_uploads(
                        session,
                        workspace_id,
                        user_id,
                        [(stored.reservation_id, stored.spec.file_path)],
                        storage,
                    )
                    raise
        except Exception as exc:
            if isinstance(exc, DomainError):
                raise
            message = "Failed to save file metadata"
            raise ServiceOperationError(message) from exc

        record_created(avatar)
        clear_search_cache(workspace_id)
        result: dict[str, object] = dict(avatar.to_dict())
        result["avatar_url"] = avatar_url
        return result


def _set_avatar_or_raise(
    session: Session, user_id: int, avatar_url: str,
) -> None:
    """Update an existing account avatar or report a missing account."""
    if not account_repository.set_avatar(session, user_id, avatar_url):
        message = "User not found"
        raise ResourceNotFoundError(message)


def upload_avatar_for_user(
    session: Session,
    actor_id: int,
    actor_role: str,
    user_id: int,
    upload: UploadSource,
) -> dict[str, object]:
    """Reject unauthorized actors before staging, then publish transactionally."""
    if actor_id != user_id and actor_role != "admin":
        message = "Permission denied"
        raise PermissionDeniedError(message)
    return AvatarUploadService(upload_settings()).upload(
        session, upload, user_id, actor_id=actor_id,
    )
