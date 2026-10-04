"""Reset an existing administrator password from the deployment terminal."""

import argparse
from getpass import getpass


def reset_admin(session, username: str, password: str):
    from app.models.user import User
    from app.features.auth.user_service import change_password
    if not 12 <= len(password) <= 128 or password.lower() in {"password1234", "123456789012"}:
        raise ValueError("Choose a unique password of at least 12 and at most 128 characters")
    user = session.query(User).filter_by(username=username, role="admin").with_for_update().populate_existing().one_or_none()
    if user is None:
        raise ValueError("Administrator not found")
    change_password(session, user.id, "admin", user.id, "", password)


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
            reset_admin(session, args.username, password)
        except ValueError as exc:
            parser.error(str(exc))
    print("Administrator password reset; previous credentials revoked")


if __name__ == "__main__":
    main()
