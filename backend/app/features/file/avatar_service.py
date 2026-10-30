"""Avatar publication across account, folder, share and file upload boundaries.

The caller owns the Session. The upload operation commits the file, share and
account URL together; this service never opens another Session or client.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from sqlalchemy.orm import Session

from app.exceptions import ResourceNotFoundError
from app.features.file import repository
from app.features.file.upload_service import UploadData, UploadSource
from app.features.share.service import CreateShareLinkParams
from app.models.file import File
from app.models.folder import Folder
from app.models.share import Share

if TYPE_CHECKING:
    from app.features.folder.service import FolderCreateData


class ShareCreator(Protocol):
    """Create the share link that publishes an avatar file."""

    def __call__(
        self, session: Session, params: CreateShareLinkParams,
    ) -> Share:
        """Create the share link of the given file and return it."""
        ...


class FileUploader(Protocol):
    """Publish one uploaded object as a file row inside the caller Session."""

    def __call__(
        self,
        session: Session,
        file_obj: UploadSource,
        data: UploadData,
        *,
        before_commit: Callable[[File], None] | None = None,
        authorize_upload: Callable[[], None] | None = None,
    ) -> File:
        """Store the upload and return the published file row."""
        ...


@dataclass(frozen=True, slots=True)
class AvatarOperations:
    """Account, folder, share and file ports used by one avatar upload."""

    authorize_account: Callable[[Session, int, int], str]
    create_folder: Callable[[Session, "FolderCreateData"], Folder]
    create_share: ShareCreator
    set_avatar: Callable[[Session, int, str], bool]
    upload_file: FileUploader
    evict_profile: Callable[[int], None]


class AvatarUploadService:
    """Publish an avatar object and point the account profile at it."""

    def __init__(self, max_bytes: int, operations: AvatarOperations) -> None:
        """Store the byte ceiling and the publication ports."""
        self.max_bytes = max_bytes
        self.operations = operations

    def upload(
        self,
        session: Session,
        upload: UploadSource,
        user_id: int,
        *,
        actor_id: int | None = None,
    ) -> dict[str, object]:
        """Store the avatar in the personal workspace and publish its URL."""
        authorizing_id = user_id if actor_id is None else actor_id

        def authorize_avatar() -> None:
            self.operations.authorize_account(session, authorizing_id, user_id)

        # Identity locks precede workspace and upload reservation locks.
        authorize_avatar()
        workspace = repository.find_owned_workspace(session, user_id)
        if workspace is None:
            message = "Personal workspace not found"
            raise ResourceNotFoundError(message)
        root = repository.find_child_folder(session, workspace.id, None)
        root_id = root.id if root else None
        folder = repository.find_child_folder(
            session, workspace.id, root_id, "所有用户头像",
        )
        if folder is None:
            folder = self.operations.create_folder(
                session,
                {
                    "name": "所有用户头像",
                    "parent_id": root_id,
                    "workspace_id": workspace.id,
                },
            )

        avatar_url: str | None = None

        def publish_metadata(avatar: File) -> None:
            nonlocal avatar_url
            share = self.operations.create_share(
                session,
                CreateShareLinkParams(user_id=user_id, file_id=avatar.id, commit=False),
            )
            avatar_url = f"/api/share/{share.token}"
            if not self.operations.set_avatar(session, user_id, avatar_url):
                message = "User not found"
                raise ResourceNotFoundError(message)

        try:
            avatar = self.operations.upload_file(
                session,
                upload,
                {
                    "workspace_id": workspace.id,
                    "uploader_id": user_id,
                    "parent_id": folder.id,
                    "max_bytes": self.max_bytes,
                },
                before_commit=publish_metadata,
                authorize_upload=authorize_avatar,
            )
        finally:
            # A lost commit acknowledgement can still leave a new SQL URL.
            self.operations.evict_profile(user_id)

        result: dict[str, object] = dict(avatar.to_dict())
        result["avatar_url"] = avatar_url
        return result
