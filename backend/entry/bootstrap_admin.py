"""Explicit first-administrator bootstrap; password is read from a terminal."""

import argparse
from getpass import getpass

from sqlalchemy import select, text


def bootstrap_admin(session, username: str, password: str):
    from app.models.user import User
    from app.models.workspace import Workspace, WorkspaceMember
    from app.models.folder import Folder

    username = username.strip()
    if not username or len(username) > 80:
        raise ValueError("Username must contain 1 to 80 characters")
    if not 12 <= len(password) <= 128 or password.lower() in {"admin123", "password1234", "123456789012"}:
        raise ValueError("Choose a unique password of at least 12 and at most 128 characters")
    with session.begin():
        # Serialize initial bootstrap across concurrent PostgreSQL processes.
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SELECT pg_advisory_xact_lock(731948210)"))
        if session.scalar(select(User.id).where(User.role == "admin").limit(1)):
            raise ValueError("An administrator already exists; use account management")
        if session.scalar(select(User.id).where(User.username == username).limit(1)):
            raise ValueError("Username already exists")
        user = User(username=username, role="admin")
        user.set_password(password)
        session.add(user)
        session.flush()
        workspace = Workspace(name=f"{username}的空间", owner_id=user.id)
        session.add(workspace)
        session.flush()
        session.add_all([
            WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="admin"),
            Folder(name="/", workspace_id=workspace.id, parent_id=None),
        ])
        user_id = user.id
    return user_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True)
    args = parser.parse_args()
    password = getpass("New administrator password: ")
    if password != getpass("Repeat password: "):
        parser.error("Passwords do not match")
    from app.infra.extensions import SessionLocal
    from app.infra.schema import check_schema
    with SessionLocal() as session:
        check_schema(session.get_bind())
        try:
            bootstrap_admin(session, args.username, password)
        except ValueError as exc:
            parser.error(str(exc))
    print("Administrator created")


if __name__ == "__main__":
    main()
