"""Avatar publication across account, folder, share and file upload boundaries.

The caller owns the Session. The upload operation commits the file, share and
account URL together; this service never opens another Session or client.
"""
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.exceptions import ResourceNotFoundError
from app.features.file import repository
from app.models.file import File
from app.models.folder import Folder
from app.models.share import Share


@dataclass(frozen=True)
class AvatarOperations:
    authorize_account: Callable[[Session, int, int], Any]
    create_folder: Callable[[Session, dict[str, Any]], Folder]
    create_share: Callable[..., Share]
    set_avatar: Callable[[Session, int, str], bool]
    upload_file: Callable[..., File]
    evict_profile: Callable[[int], None]


class AvatarUploadService:
    def __init__(self, max_bytes: int, operations: AvatarOperations):
        self.max_bytes = max_bytes
        self.operations = operations

    def upload(self, session: Session, upload: Any, user_id: int, *,
               actor_id: int | None = None) -> dict[str, Any]:
        def authorize_avatar():
            self.operations.authorize_account(session, user_id if actor_id is None else actor_id, user_id)

        # Identity locks precede workspace and upload reservation locks.
        authorize_avatar()
        workspace = repository.find_owned_workspace(session, user_id)
        if workspace is None:
            raise ResourceNotFoundError("Personal workspace not found")
        root = repository.find_child_folder(session, workspace.id, None)
        root_id = root.id if root else None
        folder = repository.find_child_folder(session, workspace.id, root_id, "所有用户头像")
        if folder is None:
            folder = self.operations.create_folder(session, {
                "name": "所有用户头像", "parent_id": root_id, "workspace_id": workspace.id})

        avatar_url = None

        def publish_metadata(avatar):
            nonlocal avatar_url
            share = self.operations.create_share(session, user_id, avatar.id, commit=False)
            avatar_url = f"/api/share/{share.token}"
            if not self.operations.set_avatar(session, user_id, avatar_url):
                raise ResourceNotFoundError("User not found")

        try:
            avatar = self.operations.upload_file(session, upload, {
                "workspace_id": workspace.id, "uploader_id": user_id,
                "parent_id": folder.id, "max_bytes": self.max_bytes},
                before_commit=publish_metadata, authorize_upload=authorize_avatar)
        finally:
            # A lost commit acknowledgement can still leave a new SQL URL.
            self.operations.evict_profile(user_id)

        result = avatar.to_dict()
        result["avatar_url"] = avatar_url
        return result
