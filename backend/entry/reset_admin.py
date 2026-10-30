"""Reset an existing administrator password from the deployment terminal."""

import argparse
import sys
from getpass import getpass

from app.features.auth.user_service import ChangePasswordParams, change_password
from app.infra.extensions import SessionLocal
from app.infra.schema import check_schema
from app.models.user import User
from sqlalchemy.orm import Session

_MIN_PASSWORD_LENGTH = 12
_MAX_PASSWORD_LENGTH = 128
_REJECTED_PASSWORDS = frozenset({"password1234", "123456789012"})


def reset_admin(session: Session, username: str, password: str) -> None:
    """Replace the administrator password and revoke previous credentials."""
    if (
        not _MIN_PASSWORD_LENGTH <= len(password) <= _MAX_PASSWORD_LENGTH
        or password.lower() in _REJECTED_PASSWORDS
    ):
        message = (
            "Choose a unique password of at least 12 and at most 128 characters"
        )
        raise ValueError(message)
    user = (
        session.query(User)
        .filter_by(username=username, role="admin")
        .with_for_update()
        .populate_existing()
        .one_or_none()
    )
    if user is None:
        message = "Administrator not found"
        raise ValueError(message)
    change_password(
        session,
        ChangePasswordParams(
            actor_id=user.id,
            actor_role="admin",
            user_id=user.id,
            old_password="",
            new_password=password,
        ),
    )


def main() -> None:
    """Parse CLI arguments and reset the administrator password."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True)
    args = parser.parse_args()
    password = getpass("New administrator password: ")
    if password != getpass("Repeat password: "):
        parser.error("Passwords do not match")
    with SessionLocal() as session:
        check_schema(session.get_bind())
        try:
            reset_admin(session, args.username, password)
        except ValueError as exc:
            parser.error(str(exc))
    sys.stdout.write("Administrator password reset; previous credentials revoked\n")


if __name__ == "__main__":
    main()
