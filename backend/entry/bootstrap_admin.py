"""Explicit first-administrator bootstrap; password is read from a terminal."""

import argparse
import sys
from getpass import getpass

from app.infra.extensions import SessionLocal
from app.infra.schema import check_schema
from app.models.folder import Folder
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from sqlalchemy import select, text
from sqlalchemy.orm import Session

_MAX_USERNAME_LENGTH = 80
_MIN_PASSWORD_LENGTH = 12
_MAX_PASSWORD_LENGTH = 128
_REJECTED_PASSWORDS = frozenset({"admin123", "password1234", "123456789012"})


def bootstrap_admin(session: Session, username: str, password: str) -> int:
    """Create the first administrator, its workspace, and the root folder."""
    username = username.strip()
    if not username or len(username) > _MAX_USERNAME_LENGTH:
        message = "Username must contain 1 to 80 characters"
        raise ValueError(message)
    if (
        not _MIN_PASSWORD_LENGTH <= len(password) <= _MAX_PASSWORD_LENGTH
        or password.lower() in _REJECTED_PASSWORDS
    ):
        message = (
            "Choose a unique password of at least 12 and at most 128 characters"
        )
        raise ValueError(message)
    with session.begin():
        # Serialize initial bootstrap across concurrent PostgreSQL processes.
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(731948210)"))
        if session.scalar(select(User.id).where(User.role == "admin").limit(1)):
            message = "An administrator already exists; use account management"
            raise ValueError(message)
        if session.scalar(select(User.id).where(User.username == username).limit(1)):
            message = "Username already exists"
            raise ValueError(message)
        user = User(username=username, role="admin")
        user.set_password(password)
        session.add(user)
        session.flush()
        workspace = Workspace(name=f"{username}的空间", owner_id=user.id)
        session.add(workspace)
        session.flush()
        session.add_all(
            [
                WorkspaceMember(
                    workspace_id=workspace.id, user_id=user.id, role="admin",
                ),
                Folder(name="/", workspace_id=workspace.id, parent_id=None),
            ],
        )
        return user.id


def main() -> None:
    """Parse CLI arguments and create the first administrator."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True)
    args = parser.parse_args()
    password = getpass("New administrator password: ")
    if password != getpass("Repeat password: "):
        parser.error("Passwords do not match")
    with SessionLocal() as session:
        check_schema(session.get_bind())
        try:
            bootstrap_admin(session, args.username, password)
        except ValueError as exc:
            parser.error(str(exc))
    sys.stdout.write("Administrator created\n")


if __name__ == "__main__":
    main()
